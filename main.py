
from flask import Flask, request, redirect, abort, send_file, jsonify
import os, re, html, json, threading, secrets, time, glob, queue
from urllib.parse import urlparse
import yt_dlp, requests

app = Flask(__name__)
app.url_map.strict_slashes = False

# ---------------- config ----------------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "*/*"}
ILLEGAL = r'[<>:"/\\|?*]'
DOWNLOAD_DIR = os.path.abspath("./downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_WORKERS = 4

for fn in os.listdir(DOWNLOAD_DIR):
    try: os.remove(os.path.join(DOWNLOAD_DIR, fn))
    except: pass

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_KEYS = {}
PENDING = []
PENDING_LOCK = threading.Lock()
TASKQ = queue.Queue()
ACTIVE = set()
ACTIVE_LOCK = threading.Lock()

# ---------------- utils ----------------
def sanitize(name: str, ext: str = ""):
    name = (name or "download").strip()
    name = re.sub(ILLEGAL, "_", name).rstrip(".")
    return f"{name}.{ext.lstrip('.')}" if ext else name

def redirect_home(reason: str):
    app.logger.warning(reason)
    return redirect("/")

COOKIES_PATH = os.path.abspath(os.environ.get("COOKIES_FILE", "./cookies.txt"))
COOKIES_OK = os.path.isfile(COOKIES_PATH)
if COOKIES_OK:
    app.logger.info(f"Using cookies file: {COOKIES_PATH}")
else:
    app.logger.warning(f"cookies.txt not found at {COOKIES_PATH}; proceeding without cookies")

def ydl_opts_base(extra=None, outtmpl=None):
    base = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "http_headers": {"User-Agent": UA},

        "paths": {"home": DOWNLOAD_DIR, "temp": DOWNLOAD_DIR},

        "outtmpl": outtmpl or os.path.join(DOWNLOAD_DIR, "%(title).200B [%(id)s].%(ext)s"),

        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 1,
        "fragment_retries": 15,
        "retries": 10,
        "continuedl": True,
        "retry_sleep_functions": {"http": {"interval": 1, "backoff": 2, "max_sleep": 10}},

        "progress_hooks": [lambda d: None],
        "postprocessor_hooks": [lambda d: None],
        "postprocessor_args": {"ffmpeg": ["-movflags", "faststart"]},
        "windowsfilenames": True,
        "cachedir": False,
    }
    if COOKIES_OK:
        base["cookiefile"] = COOKIES_PATH
    if extra:
        base.update(extra)
    return base

def new_job(kind, title="Preparing…", key=None, display_name=None):
    jid = secrets.token_hex(8)
    with JOBS_LOCK:
        JOBS[jid] = {
            "id": jid, "kind": kind, "title": title,
            "stage": "queued", "progress": 0.0, "speed": 0.0,
            "eta": None, "filename": None, "filepath": None,
            "display_name": display_name,
            "error": None, "created": time.time(), "key": key
        }
    if key:
        with JOBS_LOCK:
            JOB_KEYS[key] = jid
    return jid

def set_job(jid, **kw):
    with JOBS_LOCK:
        if jid in JOBS: JOBS[jid].update(kw)

def human_bps(bps):
    try: bps = float(bps or 0)
    except: bps = 0
    units = ["B/s","KB/s","MB/s","GB/s"]
    i = 0
    while bps >= 1024 and i < len(units)-1:
        bps /= 1024.0; i += 1
    return f"{bps:.1f} {units[i]}"

def platform_detect(url):
    host = urlparse(url).netloc.lower()
    if any(h in host for h in ("youtube.", "youtu.be", "youtube-nocookie.com", "youtubegaming.com", "music.youtube.com", "m.youtube.com")):
        return "yt"
    if "tiktok.com" in host: return "tt"
    if "soundcloud.com" in host: return "sc"
    return None

def pick_thumb(info):
    if info.get("thumbnail"): return info["thumbnail"]
    ts = info.get("thumbnails") or []
    if ts:
        ts = sorted(ts, key=lambda t: t.get("height") or 0)
        return ts[-1].get("url")
    return None

def max_height(info):
    m = 0
    for f in info.get("formats") or []:
        h = f.get("height") or 0
        if h and h > m: m = h
    return m

def best_audio_kbps(info):
    auds = [f for f in (info.get("formats") or []) if f.get("vcodec") in (None,"none") and f.get("acodec") not in (None,"none")]
    if not auds: return None
    a = max(auds, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
    return int(round(a.get("abr") or a.get("tbr") or 0)) or None

def default_sub(info):
    lang = (info.get("language") or "").split("-")[0] or None
    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    if lang and lang in subs and subs[lang]:
        s = subs[lang][0]; return (s.get("url"), s.get("ext") or "vtt", lang)
    if subs:
        k = sorted(subs.keys())[0]; s = subs[k][0]; return (s.get("url"), s.get("ext") or "vtt", k)
    if autos:
        k = "en" if "en" in autos else sorted(autos.keys())[0]
        s = autos[k][0]; return (s.get("url"), s.get("ext") or "vtt", k)
    return (None, None, None)

# ---------- file reuse ----------
def tag_for(kind):
    return {
        "yt-highest": "highest",
        "yt-hd": "hd",
        "yt-audio": "mp3",
        "tt-video": "video",
        "sc-mp3": "mp3",
    }.get(kind)

def ext_for_kind(kind):
    return {
        "yt-highest": "mp4",
        "yt-hd": "mp4",
        "yt-audio": "mp3",
        "tt-video": "mp4",
        "sc-mp3": "mp3",
    }.get(kind, "mp4")

def find_existing_by_id(vid, tag):
    pat = os.path.join(DOWNLOAD_DIR, f"*[{vid}]*[{tag}].*")
    matches = sorted(glob.glob(pat), key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0] if matches else None

def outtmpl_with_tag(tag):
    return os.path.join(DOWNLOAD_DIR, f"%(title).200B [%(id)s] [{tag}].%(ext)s")

# ---------- yt-dlp hooks ----------
def ydl_progress_hook(d):
    jid = d.get("__job_id")
    if not jid: return
    if d.get("status") == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes") or 0
        prog = (downloaded/total*100.0) if total else 0.0
        set_job(jid, stage="downloading", progress=prog, speed=d.get("speed") or 0.0, eta=d.get("eta"))
    elif d.get("status") == "finished":
        set_job(jid, stage="postprocessing", progress=100.0)

def ydl_post_hook(d):
    jid = d.get("__job_id")
    if not jid: return
    pp = d.get("postprocessor") or "postprocess"
    if d.get("status") == "started":
        set_job(jid, stage=f"{pp}…")
    elif d.get("status") == "finished":
        set_job(jid, stage=f"{pp} done")

def run_download(jid, url, opts):
    try:
        opts = dict(opts)
        def ph(d): d["__job_id"] = jid; ydl_progress_hook(d)
        def pph(d): d["__job_id"] = jid; ydl_post_hook(d)
        opts["progress_hooks"] = [ph]
        opts["postprocessor_hooks"] = [pph]
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
            fpath = info.get("_filename")
            if not fpath and "requested_downloads" in info and info["requested_downloads"]:
                fpath = info["requested_downloads"][0].get("filepath")
            if not fpath:
                vid = info.get("id")
                if vid:
                    k = JOBS.get(jid, {}).get("kind")
                    t = tag_for(k) if k else None
                    if t:
                        p = find_existing_by_id(vid, t)
                        if p: fpath = p
                    if not fpath:
                        for pth in sorted(os.listdir(DOWNLOAD_DIR)):
                            if f"[{vid}]." in pth: fpath = os.path.join(DOWNLOAD_DIR, pth)
            if not fpath or not os.path.exists(fpath):
                raise RuntimeError("Download finished but file missing")

            job = JOBS.get(jid, {})
            disp = job.get("display_name")
            if not disp:
                title = info.get("title") or "download"
                ext = ext_for_kind(job.get("kind"))
                disp = sanitize(title, ext)
            set_job(jid, stage="ready", progress=100.0, filepath=fpath,
                    filename=os.path.basename(fpath), display_name=disp)
    except Exception as e:
        set_job(jid, stage="error", error=str(e))
    finally:
        with ACTIVE_LOCK:
            ACTIVE.discard(jid)

# ---------- job queue / workers ----------
def queue_position(jid):
    with PENDING_LOCK:
        try:
            return PENDING.index(jid) + 1
        except ValueError:
            return 0

def enqueue_job(jid, url, opts):
    with PENDING_LOCK:
        PENDING.append(jid)
    TASKQ.put((jid, url, opts))
    set_job(jid, stage="queued")

def worker_loop():
    while True:
        jid, url, opts = TASKQ.get()
        while True:
            with ACTIVE_LOCK:
                if len(ACTIVE) < MAX_WORKERS:
                    ACTIVE.add(jid)
                    break
            time.sleep(0.2)
        with PENDING_LOCK:
            try: PENDING.remove(jid)
            except ValueError: pass
        set_job(jid, stage="starting")
        run_download(jid, url, opts)
        TASKQ.task_done()

for _ in range(max(1, MAX_WORKERS)):
    threading.Thread(target=worker_loop, daemon=True).start()

# ---------- UI ----------
def page_shell(body_html, title=""):
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0b0c">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="format-detection" content="telephone=no,email=no,address=no">

<title>{html.escape(title or "DownTil")}</title>
<style>
:root {{
  --bg:#0b0b0c; --card:#111114; --card2:#17171a; --text:#f5f5f7; --muted:#bdbdc2; --accent:#7d7dfb; --accent2:#9f67ff; --border:#232329;
}}
*{{box-sizing:border-box}}
html, body {{height:100%; margin:0;}}
body {{
  display:flex; flex-direction:column; min-height:100%;
  background:linear-gradient(180deg,#0b0b0c 0,#0e0e12 100%);
  background-attachment:fixed; background-repeat:no-repeat; background-size:cover;
  color:var(--text);
  font-family:-apple-system, BlinkMacSystemFont, 'SF Pro Text', Segoe UI, Roboto, Arial, sans-serif;
}}
/* CONTAINER */
.wrap{{max-width:980px; width:100%; margin:24px auto; padding:0 16px; flex:1 0 auto;}}

/* SEARCH */
.search{{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:10px 12px;margin-bottom:16px;}}
.search input{{width:100%;background:transparent;border:0;outline:0;color:var(--text);font-size:16px}}

/* CARDS */
.card{{background:var(--card2);border:1px solid var(--border);border-radius:18px;padding:18px;box-shadow:0 10px 40px #0006}}
.row{{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap}}
.thumb{{width:320px;max-width:100%;aspect-ratio:16/9;border-radius:14px;border:1px solid var(--border);object-fit:cover;background:#000}}
.meta{{flex:1;min-width:260px}}
h1{{margin:0 0 6px;font-size:20px;letter-spacing:.2px}}
h2{{margin:0;color:var(--muted);font-size:14px;font-weight:500}}
.btns{{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}}
.btn{{appearance:none;border:1px solid var(--border);background:linear-gradient(180deg,#1d1d22,#15151a);color:var(--text);padding:10px 14px;border-radius:12px;text-decoration:none;display:inline-flex;gap:8px;align-items:center;justify-content:center}}
.btn:hover{{border-color:#2e2e35;background:linear-gradient(180deg,#22222a,#18181f)}}
.small{{font-size:12px;color:var(--muted)}}
.footer{{opacity:.6;font-size:12px;margin:12px 0 12px;text-align:center}}
.progress-card{{background:var(--card2);border:1px solid var(--border);border-radius:16px;padding:18px;}}
.bar-wrap{{height:12px;background:#121217;border:1px solid var(--border);border-radius:999px;overflow:hidden}}
.bar{{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0%}}
a.link{{color:#a7b3ff;text-decoration:none}}

/* --- RESPONSIVE --- */
@media (max-width: 800px) {{
  .row{{flex-direction:column}}
  .thumb{{width:100%}}
  .meta{{min-width:0}}
}}
@media (max-width: 560px) {{
  .btns{{display:grid; grid-template-columns:1fr 1fr; gap:10px}}
}}
@media (max-width: 380px) {{
  .btns{{grid-template-columns:1fr}}
}}
/* iOS safe areas */
@supports (padding: env(safe-area-inset-top)) {{
  body{{padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right)}}
}}
</style>
</head><body>
<div class="wrap">
  <form class="search" action="/" method="get" id="searchForm">
    <input id="qinput" type="url" name="q" placeholder="Paste YouTube / TikTok / SoundCloud URL…" value="{html.escape(request.args.get('q') or '')}">
  </form>
  {body_html}
  <div class="footer">Files are processed on server. Already-processed files are cached and reused.</div>
</div>
<script>
(function(){{
  const f = document.getElementById('searchForm');
  const input = document.getElementById('qinput');
  const isValid = (u) => {{
    try {{
      const url = new URL(u);
      const h = url.hostname.toLowerCase();
      if (!/^https?:$/.test(url.protocol)) return false;
      return (h.includes('youtube.') || h==='youtu.be' || h.includes('youtube-nocookie.com') ||
              h.includes('music.youtube.com') || h.includes('youtubegaming.com') || h.includes('m.youtube.com') ||
              h.includes('tiktok.com') || h.includes('soundcloud.com'));
    }} catch {{ return false; }}
  }};
  f.addEventListener('submit', (e) => {{
    const v = (input.value || '').trim();
    if (!v || !isValid(v)) {{
      e.preventDefault();
      alert('Please paste a valid YouTube, TikTok, or SoundCloud link.');
      return false;
    }}
  }});
}})();
</script>
</body></html>"""

def detail_page(info, buttons):
    title = info.get("title") or "Untitled"
    creator = info.get("uploader") or info.get("channel") or info.get("artist") or "Unknown"
    thumb = pick_thumb(info)
    btn_html = "".join([f'<a class="btn" href="{html.escape(href)}">{html.escape(lbl)}</a>' for lbl, href in buttons])
    body = f"""
    <div class="card">
      <div class="row">
        {'<img class="thumb" src="'+html.escape(thumb)+'" />' if thumb else ''}
        <div class="meta">
          <h1>{html.escape(title)}</h1>
          <h2>{html.escape(creator)}</h2>
          <div class="btns">{btn_html}</div>
        </div>
      </div>
    </div>
    """
    return page_shell(body, f"{title} - {creator}")

def job_page(jid):
    j = JOBS.get(jid)
    if not j: abort(404)
    title = j.get("title") or "Processing…"
    own = "1" if (request.args.get("own") == "1") else "0"
    body = f"""
    <div class="card">
      <div class="row" style="align-items:stretch">
        <div class="meta" style="width:100%">
          <h1>{html.escape(title)}</h1>
          <div class="progress-card" style="margin-top:10px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
              <div class="small"><span id="stage">{html.escape(j.get('stage') or '')}</span></div>
              <div class="small">Queue: <span id="qpos">{"0"}</span></div>
            </div>
            <div class="bar-wrap"><div id="bar" class="bar" style="width:{j.get('progress'):.1f}%"></div></div>
            <div class="small" style="margin-top:8px">
              <span id="pct">{j.get('progress'):.1f}%</span> •
              <span id="speed">{human_bps(j.get('speed'))}</span>
              <span id="eta"></span>
            </div>
            <div id="done" style="margin-top:12px;display:none">
                <a class="btn" id="download" href="#">Download</a>
            </div>
            <div id="err" class="small" style="margin-top:10px;color:#ff9a9a;display:none"></div>
          </div>
        </div>
      </div>
    </div>
    <script>
    const jid = {json.dumps(jid)};
    const owner = {own};
    const bar = document.getElementById('bar');
    const pct = document.getElementById('pct');
    const stage = document.getElementById('stage');
    const speed = document.getElementById('speed');
    const eta = document.getElementById('eta');
    const qpos = document.getElementById('qpos');
    const done = document.getElementById('done');
    const err = document.getElementById('err');
    const dl = document.getElementById('download');
    async function poll(){{
      try {{
        const r = await fetch('/job/'+jid+'/status');
        const j = await r.json();
        if (j.error) {{ err.style.display='block'; err.textContent=j.error; return; }}
        qpos.textContent = j.queue_position || 0;
        bar.style.width = (j.progress||0).toFixed(1)+'%';
        pct.textContent = (j.progress||0).toFixed(1)+'%';
        stage.textContent = j.stage || '';
        speed.textContent = j.speed_human || '';
        eta.textContent = (j.eta !== null && j.eta !== undefined) ? (' • ETA ' + Number(j.eta).toFixed(2) + 's') : '';
        if (j.ready && j.file_url) {{
          done.style.display='block';
          dl.href = j.file_url;
          if (owner === 1) {{
            window.location.href = j.file_url; // auto-download for creator
          }}
          return;
        }}
      }} catch(e) {{
        err.style.display='block'; err.textContent='Lost connection.'; return;
      }}
      setTimeout(poll, 600);
    }}
    poll();
    </script>
    """
    return page_shell(body, "Processing…")

# ---------- homepage ----------
@app.route("/")
def home():
    q = (request.args.get("q") or "").strip()
    if not q:
        body = """
        <div class="card">
          <div class="row">
            <div class="meta">
              <h1>Paste a link above</h1>
              <h2 class="small">Download content YouTube, TikTok, and Soundcloud. Processed server-side</h2>
            </div>
          </div>
        </div>
        """
        return page_shell(body, "DownTil")
    if not re.match(r"^https?://", q, re.I): return redirect_home("home: bad URL in ?q")
    p = platform_detect(q)
    if p == "yt": return redirect("/yt?url=" + requests.utils.quote(q, safe=""))
    if p == "tt": return redirect("/tt?url=" + requests.utils.quote(q, safe=""))
    if p == "sc": return redirect("/sc?url=" + requests.utils.quote(q, safe=""))
    return redirect_home("home: unsupported URL")

# ---------- YouTube ----------
@app.route("/yt")
def yt_by_url():
    url = request.args.get("url","").strip()
    if not url or not re.match(r"^https?://", url, re.I):
        return redirect_home("yt: missing or invalid ?url")
    try:
        with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
            info = y.extract_info(url, download=False)
    except Exception as e:
        return redirect_home(f"yt: extractor failed for {url!r}; {e}")
    return yt_detail(info)

@app.route("/yt/<vid>")
def yt_detail_by_id(vid):
    try:
        with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    except Exception as e:
        return redirect_home(f"yt: invalid video id={vid!r}; {e}")
    return yt_detail(info)

def yt_detail(info):
    vid = info.get("id")
    mh = max_height(info)
    buttons = []
    if mh > 1080: buttons.append((f"Highest ({mh}p)", f"/yt/{vid}/start/highest"))
    buttons.append(("HD (≤1080p)", f"/yt/{vid}/start/hd"))
    kb = best_audio_kbps(info)
    buttons.append((f"Audio ({kb} kbps)" if kb else "Audio (best)", f"/yt/{vid}/start/audio"))
    s_url, s_ext, s_lang = default_sub(info)
    if s_url: buttons.append((f"Subtitles ({s_lang.upper()})", f"/yt/{vid}/subs"))
    if pick_thumb(info): buttons.append(("Thumbnail", f"/yt/{vid}/thumb"))
    return detail_page(info, buttons)

@app.route("/yt/<vid>/thumb")
def yt_thumb(vid):
    with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    t = pick_thumb(info)
    if not t: abort(404)
    r = requests.get(t, headers=HEADERS, stream=True, timeout=20)
    if r.status_code >= 400: abort(404)
    ct = r.headers.get("Content-Type","image/jpeg")
    ext = "jpg"
    if "png" in ct: ext="png"
    if "webp" in ct: ext="webp"
    fname = sanitize(info.get("title") or "thumbnail", ext)
    return send_file(r.raw, mimetype=ct, as_attachment=True, download_name=fname)

@app.route("/yt/<vid>/subs")
def yt_subs(vid):
    with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    s_url, s_ext, s_lang = default_sub(info)
    if not s_url: abort(404, "No subtitles")
    r = requests.get(s_url, headers=HEADERS, stream=True, timeout=20)
    if r.status_code >= 400: abort(404)
    fname = sanitize(f"{info.get('title') or 'subtitles'} [{s_lang}]", s_ext or "vtt")
    return send_file(r.raw, mimetype="text/vtt", as_attachment=True, download_name=fname)

def yt_opts(mode):
    if mode == "highest":
        fmt = "bv*+ba/b"
    elif mode == "hd":
        fmt = "bv*[height<=1080]+ba/b[height<=1080]"
    elif mode == "audio":
        fmt = "bestaudio/best"
    else:
        raise ValueError("bad mode")

    opts = {"format": fmt}
    if mode == "audio":
        opts["postprocessors"] = [
            {"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"0"},
            {"key":"FFmpegMetadata"}
        ]
    else:
        opts["postprocessors"] = [
            {"key":"FFmpegVideoRemuxer","preferedformat":"mp4"}
        ]
    return opts


def job_key(kind, info_id):
    return f"{kind}:{info_id}"

def reuse_or_redirect(kind, info, title, url, opts, owner=True):
    vid = info.get("id")
    tag = tag_for(kind)
    disp = sanitize(info.get("title") or "download", ext_for_kind(kind))

    if tag:
        existing = find_existing_by_id(vid, tag)
        if existing:
            jid = new_job(kind, title=title, key=job_key(kind, vid), display_name=disp)
            set_job(jid, stage="ready", progress=100.0, filepath=existing,
                    filename=os.path.basename(existing))
            return redirect(f"/job/{jid}?own={'1' if owner else '0'}")

    key = job_key(kind, vid)
    with JOBS_LOCK:
        other = JOB_KEYS.get(key)
        if other and other in JOBS and not JOBS[other].get("error"):
            return redirect(f"/job/{other}?own=0")

    outtmpl = outtmpl_with_tag(tag) if tag else None
    dl_opts = ydl_opts_base(opts, outtmpl=outtmpl)
    jid = new_job(kind, title=title, key=key, display_name=disp)
    enqueue_job(jid, url, dl_opts)
    return redirect(f"/job/{jid}?own={'1' if owner else '0'}")

@app.route("/yt/<vid>/start/<mode>")
def yt_start(vid, mode):
    url = f"https://www.youtube.com/watch?v={vid}"
    with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
        info = y.extract_info(url, download=False)
    title = f"YouTube - {info.get('title') or 'Video'}"
    return reuse_or_redirect(f"yt-{mode}", info, title, url, yt_opts(mode), owner=True)

# ---------- TikTok ----------
@app.route("/tt")
def tt_by_url():
    url = request.args.get("url","").strip()
    if not url or not re.match(r"^https?://", url, re.I):
        return redirect_home("tt: missing or invalid ?url param; redirecting home")
    try:
        with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
            info = y.extract_info(url, download=False)
    except Exception as e:
        return redirect_home(f"tt: extractor failed for url={url!r}; {e}")
    return tt_detail(info)

@app.route("/tt/<anyid>")
def tt_by_id(anyid):
    url = f"https://www.tiktok.com/@_/video/{anyid}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
            info = y.extract_info(url, download=False)
    except Exception as e:
        return redirect_home(f"tt: invalid id={anyid!r}; {e}")
    return tt_detail(info)

def tt_detail(info):
    vid = info.get("id")
    buttons = [("Download Video", f"/tt/{vid}/start/video")]
    if pick_thumb(info): buttons.append(("Thumbnail", f"/tt/{vid}/thumb"))
    return detail_page(info, buttons)

@app.route("/tt/<vid>/thumb")
def tt_thumb(vid):
    with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
        info = y.extract_info(f"https://www.tiktok.com/@_/video/{vid}", download=False)
    t = pick_thumb(info)
    if not t: abort(404)
    r = requests.get(t, headers=HEADERS, stream=True, timeout=20)
    if r.status_code >= 400: abort(404)
    ct = r.headers.get("Content-Type","image/jpeg")
    ext = "jpg"
    if "png" in ct: ext="png"
    if "webp" in ct: ext="webp"
    fname = sanitize(info.get("title") or info.get("description") or "tiktok", ext)
    return send_file(r.raw, mimetype=ct, as_attachment=True, download_name=fname)

@app.route("/tt/<vid>/start/video")
def tt_start_video(vid):
    url = f"https://www.tiktok.com/@_/video/{vid}"
    with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
        info = y.extract_info(url, download=False)
    title = f"TikTok - {info.get('title') or info.get('description') or 'Video'}"
    return reuse_or_redirect("tt-video", info, title, url,
                             {"format": "bv*+ba/b",
                              {"key":"FFmpegVideoRemuxer","preferedformat":"mp4"}:None} if False else
                             {"format":"bv*+ba/b","postprocessors":[{"key":"FFmpegVideoRemuxer","preferedformat":"mp4"}]},
                             owner=True)

# ---------- SoundCloud ----------
@app.route("/sc")
def sc_by_url():
    url = request.args.get("url","").strip()
    if not url or not re.match(r"^https?://", url, re.I):
        return redirect_home("sc: missing or invalid ?url param; redirecting home")
    try:
        with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
            info = y.extract_info(url, download=False)
    except Exception as e:
        return redirect_home(f"sc: extractor failed for url={url!r}; {e}")
    return sc_detail(info)

@app.route("/sc/<user>/<track>")
def sc_detail_route(user, track):
    url = f"https://soundcloud.com/{user}/{track}"
    with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
        info = y.extract_info(url, download=False)
    return sc_detail(info)

def sc_detail(info):
    buttons = [("Download MP3", f"/sc/{info.get('id')}/start/mp3")]
    if pick_thumb(info): buttons.append(("Cover", f"/sc/{info.get('id')}/cover"))
    return detail_page(info, buttons)

@app.route("/sc/<sid>/cover")
def sc_cover(sid):
    with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
        info = y.extract_info(f"https://api.soundcloud.com/tracks/{sid}", download=False)
    t = pick_thumb(info)
    if not t: abort(404)
    r = requests.get(t, headers=HEADERS, stream=True, timeout=20)
    if r.status_code >= 400: abort(404)
    ct = r.headers.get("Content-Type","image/jpeg")
    ext = "jpg"
    if "png" in ct: ext="png"
    if "webp" in ct: ext="webp"
    fname = sanitize(f"{info.get('uploader') or 'Artist'} - {info.get('title') or 'cover'}", ext)
    return send_file(r.raw, mimetype=ct, as_attachment=True, download_name=fname)

@app.route("/sc/<sid>/start/mp3")
def sc_start_mp3(sid):
    url = f"https://api.soundcloud.com/tracks/{sid}"
    with yt_dlp.YoutubeDL(ydl_opts_base()) as y:
        info = y.extract_info(url, download=False)
    title = f"SoundCloud - {info.get('title') or 'Track'}"
    return reuse_or_redirect("sc-mp3", info, title, url, {
        "format": "bestaudio/best",
        "addmetadata": True, "writethumbnail": True,
        "postprocessors": [
            {"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"0"},
            {"key":"FFmpegMetadata"},
            {"key":"EmbedThumbnail"},
        ]
    }, owner=True)

# ---------- Jobs ----------
@app.route("/job/<jid>")
def job_view(jid):
    return job_page(jid)

@app.route("/job/<jid>/status")
def job_status(jid):
    j = JOBS.get(jid)
    if not j: return jsonify({"error":"unknown job"}), 404
    eta_val = j.get("eta")
    try:
        eta_val = round(float(eta_val), 2) if eta_val is not None else None
    except Exception:
        eta_val = None
    return jsonify({
        "id": j["id"],
        "stage": j["stage"],
        "progress": j["progress"],
        "speed": j["speed"],
        "speed_human": human_bps(j["speed"]),
        "eta": eta_val,
        "queue_position": queue_position(jid),
        "ready": (j.get("filepath") is not None and j.get("error") is None),
        "file_url": (f"/job/{jid}/file" if j.get("filepath") else None),
        "error": j.get("error"),
    })
    
@app.route("/job/<jid>/file")
def job_file(jid):
    j = JOBS.get(jid)
    if not j or not j.get("filepath"): abort(404)
    disp = j.get("display_name")
    if not disp:
        title_guess = (j.get("title") or "download").split(" - ", 1)[-1]
        ext = os.path.splitext(j["filepath"])[1].lstrip(".") or "bin"
        disp = sanitize(title_guess, ext)
    return send_file(j["filepath"], as_attachment=True, download_name=disp)

app.run(host='0.0.0.0', port=80, debug=False)
