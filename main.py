"""DownTil: minimal Flask media downloader."""
import html
import importlib.metadata
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import requests
import yt_dlp
from flask import Flask, abort, jsonify, redirect, request, send_file, url_for
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, TPOS

app = Flask(__name__)
app.url_map.strict_slashes = False
ROOT = Path(os.getenv('DOWNLOAD_DIR', 'downloads')).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
COOKIES = Path(os.getenv('COOKIES_FILE', 'cookies.txt')).resolve()
WORKERS = max(1, min(8, int(os.getenv('MAX_WORKERS', str(min(4, os.cpu_count() or 2))))))
CACHE_HOURS = max(1, int(os.getenv('CACHE_HOURS', '6')))
POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='downtil')
LOCK = threading.RLock()
JOBS = {}
KEYS = {}
METADATA = OrderedDict()
LOG = logging.getLogger('downtil')
DOMAINS = {'yt': ('youtube.com', 'youtu.be', 'youtube-nocookie.com', 'youtubegaming.com'), 'tt': ('tiktok.com',), 'sc': ('soundcloud.com',)}
KINDS = {'yt-highest': ('highest', 'mp4'), 'yt-hd': ('hd', 'mp4'), 'yt-audio': ('mp3', 'mp3'), 'tt-video': ('video', 'mp4'), 'sc-mp3': ('mp3', 'mp3')}


def validate_url(raw, expected=None):
    raw = (raw or '').strip()
    if raw.startswith('www.'):
        raw = 'https://' + raw
    if len(raw) > 2048 or any(c.isspace() for c in raw):
        raise ValueError('Please paste a shorter, valid link.')
    try:
        p = urlsplit(raw)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme not in ('http', 'https') or p.username or p.password or p.port:
            raise ValueError()
    except ValueError:
        raise ValueError('Please paste a valid YouTube, TikTok, or SoundCloud link.') from None
    for kind, domains in DOMAINS.items():
        if any(host == d or host.endswith('.' + d) for d in domains) and (not expected or expected == kind):
            return urlunsplit((p.scheme, p.netloc, p.path, p.query, '')), kind
    raise ValueError('Only YouTube, TikTok, and SoundCloud links are supported.')


def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(value or '')):
        abort(400, 'Invalid media ID.')
    return str(value)


def clean_filename(name, extension=''):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name or 'download')).strip(' .')[:180] or 'download'
    return name + ('.' + extension.lstrip('.') if extension else '')


def base_opts(extra=None, template=None):
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'paths': {'home': str(ROOT), 'temp': str(ROOT)},
            'outtmpl': template or str(ROOT / '%(title).180B [%(id)s].%(ext)s'), 'windowsfilenames': True,
            'retries': 5, 'fragment_retries': 5, 'concurrent_fragment_downloads': 1,
            'merge_output_format': 'mp4', 'socket_timeout': 20, 'cachedir': False}
    if COOKIES.is_file():
        opts['cookiefile'] = str(COOKIES)
    opts.update(extra or {})
    return opts


def extract(url):
    with LOCK:
        cached = METADATA.get(url)
        if cached and time.monotonic() - cached[0] < 300:
            METADATA.move_to_end(url)
            return cached[1]
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict) or info.get('_type') in ('playlist', 'multi_video'):
        raise ValueError('Please select one video or track, not a playlist.')
    with LOCK:
        METADATA[url] = (time.monotonic(), info)
        METADATA.move_to_end(url)
        while len(METADATA) > 64:
            METADATA.popitem(last=False)
    return info


def media_url(kind, item_id):
    item_id = safe_id(item_id)
    return {'yt': f'https://www.youtube.com/watch?v={item_id}', 'tt': f'https://www.tiktok.com/@_/video/{item_id}',
            'sc': f'https://api.soundcloud.com/tracks/{item_id}'}[kind]


def existing_file(item_id, tag):
    suffix = f' [{item_id}] [{tag}]'
    matches = (p for p in ROOT.iterdir() if p.is_file() and p.stem.endswith(suffix) and p.suffix.lower() in ('.mp3', '.mp4'))
    return max(matches, key=lambda p: p.stat().st_mtime, default=None)


def thumb(info):
    images = info.get('thumbnails') or []
    return info.get('thumbnail') or (max(images, key=lambda t: t.get('height') or 0).get('url') if images else None)


def subtitles(info):
    preferred = str(info.get('language') or '').split('-')[0]
    for tracks in (info.get('subtitles') or {}, info.get('automatic_captions') or {}):
        if tracks:
            lang = preferred if preferred in tracks else 'en' if 'en' in tracks else next(iter(tracks))
            for track in tracks.get(lang) or []:
                if track.get('url'):
                    return track['url'], track.get('ext') or 'vtt', lang
    return None


def format_opts(kind, info):
    if kind in ('yt-audio', 'sc-mp3'):
        opts = {'format': 'bestaudio/best', 'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '0'}, {'key': 'FFmpegMetadata'}]}
        if kind == 'sc-mp3':
            opts['writethumbnail'] = True
            opts['postprocessors'].append({'key': 'EmbedThumbnail'})
        return opts
    if kind == 'tt-video':
        return {'format': 'bv*+ba/b', 'merge_output_format': 'mp4', 'postprocessors': [{'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4'}]}
    cap = '[height<=1080]' if kind == 'yt-hd' else ''
    compatible = any(str(f.get('vcodec') or '').startswith('avc1') and f.get('ext') == 'mp4' and
                     (kind != 'yt-hd' or (f.get('height') or 0) <= 1080) for f in info.get('formats') or [])
    if compatible:
        return {'format': f'bv*{cap}[vcodec^=avc1][ext=mp4]+ba[ext=m4a]/b{cap}[ext=mp4]/bv*{cap}+ba/b', 'merge_output_format': 'mp4'}
    return {'format': f'bv*{cap}+ba/b{cap}', 'merge_output_format': 'mp4',
            'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]}


def tag_soundcloud(path, info):
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        title = str(info.get('title') or path.stem)
        artist = str(info.get('artist') or info.get('uploader') or 'Unknown')
        fields = {'TIT2': TIT2(encoding=3, text=title), 'TPE1': TPE1(encoding=3, text=artist),
                  'TPE2': TPE2(encoding=3, text=artist),
                  'TALB': TALB(encoding=3, text=str(info.get('album') or f'{title} - Single')),
                  'TRCK': TRCK(encoding=3, text=str(info.get('track_number') or 1)),
                  'TPOS': TPOS(encoding=3, text=str(info.get('disc_number') or 1))}
        genre = str(info.get('genre') or '').strip()
        if genre and len(genre) <= 64 and genre.casefold() != artist.casefold():
            fields['TCON'] = TCON(encoding=3, text=genre)
        date = str(info.get('release_date') or info.get('upload_date') or '')
        if re.fullmatch(r'\d{8}', date):
            fields['TDRC'] = TDRC(encoding=3, text=f'{date[:4]}-{date[4:6]}-{date[6:]}')
        for key, frame in fields.items():
            tags.delall(key)
            tags.add(frame)
        tags.save(path)
    except Exception:
        LOG.exception('Could not write SoundCloud tags')


def user_error(exc):
    if isinstance(exc, requests.Timeout):
        return 'The media service timed out. Please try again.'
    if isinstance(exc, requests.RequestException):
        return 'Could not reach the media service. Please try again.'
    if isinstance(exc, yt_dlp.utils.DownloadError):
        return str(exc).removeprefix('ERROR: ').strip()[:350] or 'The media could not be downloaded.'
    if isinstance(exc, ValueError):
        return str(exc)
    return 'Something went wrong on the server. Please try again.'


def update_job(jid, **changes):
    with LOCK:
        if jid in JOBS:
            JOBS[jid].update(changes)


def download(jid, url, info, kind):
    tag, ext = KINDS[kind]
    last = 0
    def progress(data):
        nonlocal last
        now = time.monotonic()
        if data.get('status') == 'downloading' and now - last >= .3:
            last = now
            total = data.get('total_bytes') or data.get('total_bytes_estimate') or 0
            update_job(jid, stage='downloading', progress=round(100 * (data.get('downloaded_bytes') or 0) / total, 1) if total else 0,
                       speed=data.get('speed') or 0, eta=data.get('eta'))
        elif data.get('status') == 'finished':
            update_job(jid, stage='processing', progress=100)
    update_job(jid, stage='starting')
    opts = base_opts(format_opts(kind, info), str(ROOT / f'%(title).180B [%(id)s] [{tag}].%(ext)s'))
    opts['progress_hooks'] = [progress]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(url, download=True)
        file = existing_file(str(info['id']), tag)
        if file is None:
            raise RuntimeError('Download completed but no output file was found.')
        if kind == 'sc-mp3':
            tag_soundcloud(file, result or info)
        update_job(jid, stage='ready', progress=100, path=str(file))
    except Exception as exc:
        LOG.exception('Download %s failed', jid)
        update_job(jid, stage='error', error=user_error(exc))


def start_job(kind, info, url):
    item_id = safe_id(str(info.get('id') or ''))
    tag, ext = KINDS[kind]
    key = f'{kind}:{item_id}'
    with LOCK:
        previous = KEYS.get(key)
        if previous and previous in JOBS and JOBS[previous]['path'] and not Path(JOBS[previous]['path']).is_file():
            JOBS[previous].update(path=None, stage='expired', error='This download has expired. Please start it again.')
        if previous and previous in JOBS and JOBS[previous]['stage'] not in ('expired', 'error'):
            return redirect(url_for('job_view', jid=previous, own='0'))
        file = existing_file(item_id, tag)
        jid = secrets.token_hex(16)
        JOBS[jid] = {'id': jid, 'key': key, 'title': str(info.get('title') or 'Download'), 'created': time.time(),
                     'stage': 'ready' if file else 'queued', 'progress': 100 if file else 0,
                     'speed': 0, 'eta': None, 'path': str(file) if file else None, 'error': None,
                     'filename': clean_filename(info.get('title') or 'download', ext)}
        KEYS[key] = jid
        if not file:
            POOL.submit(download, jid, url, info, kind)
    return redirect(url_for('job_view', jid=jid, own='1'))


def page_shell(body, title='DownTil', footer='Files are processed on the server. Finished downloads are cached and reused.'):
    esc = lambda s: html.escape(str(s), quote=True)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0b0b0c"><link rel="icon" href="{url_for('static', filename='favicon.png')}"><title>{esc(title)}</title><style>
:root{{--bg:#0b0b0c;--card:#111114;--card2:#17171a;--text:#f5f5f7;--muted:#bdbdc2;--accent:#7d7dfb;--accent2:#9f67ff;--border:#232329}}
*{{box-sizing:border-box}}html,body{{height:100%;margin:0}}body{{display:flex;flex-direction:column;min-height:100%;background:linear-gradient(180deg,#0b0b0c,#0e0e12);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif}}
.wrap{{max-width:980px;width:100%;margin:24px auto;padding:0 16px;flex:1 0 auto}}.search{{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:10px 12px;margin-bottom:16px;display:flex;gap:10px}}.search input{{width:100%;min-width:0;background:transparent;border:0;outline:0;color:var(--text);font-size:16px}}
.card{{background:var(--card2);border:1px solid var(--border);border-radius:18px;padding:18px;box-shadow:0 10px 40px #0006}}.row{{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap}}.thumb{{width:320px;max-width:100%;aspect-ratio:16/9;border-radius:14px;border:1px solid var(--border);object-fit:contain;background:#000}}.meta{{flex:1;min-width:260px}}h1{{margin:0 0 6px;font-size:20px}}h2{{margin:0;color:var(--muted);font-size:14px;font-weight:500;overflow-wrap:anywhere}}
.btns{{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}}.btn{{appearance:none;border:1px solid var(--border);background:linear-gradient(180deg,#1d1d22,#15151a);color:var(--text);padding:10px 14px;border-radius:12px;text-decoration:none;display:inline-flex;gap:8px;align-items:center;justify-content:center;cursor:pointer;font:inherit}}.btn:hover{{border-color:#46465a;background:#22222a}}.btn:focus-visible,.search input:focus-visible{{outline:2px solid var(--accent);outline-offset:3px}}.small{{font-size:12px;color:var(--muted)}}.footer{{opacity:.6;font-size:12px;margin:12px 0;text-align:center}}.progress-card{{background:var(--card2);border:1px solid var(--border);border-radius:16px;padding:18px}}.bar-wrap{{height:12px;background:#121217;border:1px solid var(--border);border-radius:999px;overflow:hidden}}.bar{{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0%;transition:width .2s}}.error{{color:#ff9a9a;overflow-wrap:anywhere}}
@media(max-width:800px){{.row{{flex-direction:column}}.thumb{{width:100%}}.meta{{min-width:0;width:100%}}}}@media(max-width:560px){{.btns{{display:grid;grid-template-columns:1fr 1fr}}}}@media(max-width:380px){{.btns{{grid-template-columns:1fr}}}}@media(prefers-reduced-motion:reduce){{.bar{{transition:none}}}}</style></head><body><main class="wrap"><form class="search" action="/" method="get"><input id="qinput" type="url" name="q" aria-label="Media URL" placeholder="Paste YouTube / TikTok / SoundCloud URL…" value="{esc(request.args.get('q', ''))}" required><button class="btn" type="submit">Go</button></form>{body}<div class="footer">{esc(footer)}</div></main></body></html>'''


def error_page(message, status=400):
    return page_shell(f'<div class="card"><h1>Could not process that link</h1><p class="error" role="alert">{html.escape(message)}</p><a class="btn" href="/">Try another link</a></div>', 'DownTil - Error'), status


@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if request.path.startswith('/job/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/')
def home():
    query = request.args.get('q', '').strip()
    if not query:
        return page_shell('<div class="card"><div class="row"><div class="meta"><h1>Paste a link above</h1><h2 class="small">Download content from YouTube, TikTok, and SoundCloud. Processed server-side.</h2></div></div></div>')
    try:
        url, kind = validate_url(query)
    except ValueError as exc:
        return error_page(str(exc))
    return redirect(url_for({'yt': 'yt_by_url', 'tt': 'tt_by_url', 'sc': 'sc_by_url'}[kind], url=url))


def detail_for(kind, info):
    item_id = safe_id(str(info.get('id') or ''))
    image = thumb(info)
    title = str(info.get('title') or 'Untitled')
    creator = str(info.get('uploader') or info.get('channel') or info.get('artist') or 'Unknown')
    if kind == 'yt':
        height = max((f.get('height') or 0 for f in info.get('formats') or []), default=0)
        buttons = []
        if height > 1080:
            buttons.append((f'Highest ({height}p)', url_for('yt_start', vid=item_id, mode='highest')))
        buttons += [('HD (≤1080p)', url_for('yt_start', vid=item_id, mode='hd')),
                    ('Audio (best)', url_for('yt_start', vid=item_id, mode='audio'))]
        sub = subtitles(info)
        if sub:
            buttons.append((f'Subtitles ({sub[2].upper()})', url_for('yt_subs', vid=item_id)))
        if image:
            buttons.append(('Thumbnail', url_for('yt_thumb', vid=item_id)))
        media = f'<iframe class="thumb" src="https://www.youtube.com/embed/{quote(item_id)}?rel=0" title="YouTube video player" loading="lazy" allow="picture-in-picture; encrypted-media" allowfullscreen></iframe>'
    elif kind == 'tt':
        buttons = [('Download Video', url_for('tt_start_video', vid=item_id))]
        if image:
            buttons.append(('Thumbnail', url_for('tt_thumb', vid=item_id)))
        media = f'<img class="thumb" src="{html.escape(image or "", quote=True)}" loading="lazy" alt="Media cover">' if image else ''
    else:
        buttons = [('Download MP3', url_for('sc_start_mp3', sid=item_id))]
        if image:
            buttons.append(('Cover', url_for('sc_cover', sid=item_id)))
        media = f'<img class="thumb" src="{html.escape(image or "", quote=True)}" loading="lazy" alt="Media cover">' if image else ''
    controls = ''.join(f'<a class="btn" href="{html.escape(href, quote=True)}">{html.escape(label)}</a>' for label, href in buttons)
    return page_shell(f'<div class="card"><div class="row">{media}<div class="meta"><h1>{html.escape(title)}</h1><h2>{html.escape(creator)}</h2><div class="btns">{controls}</div></div></div></div>', f'{title} - {creator}')


def get_detail(url, kind):
    try:
        url, _ = validate_url(url, kind)
        return detail_for(kind, extract(url))
    except Exception as exc:
        LOG.warning('Metadata lookup failed: %s', exc)
        return error_page(user_error(exc), 400 if isinstance(exc, ValueError) else 502)


@app.route('/yt')
def yt_by_url():
    return get_detail(request.args.get('url', ''), 'yt')


@app.route('/yt/<vid>')
def yt_detail_by_id(vid):
    return get_detail(media_url('yt', vid), 'yt')


@app.route('/tt')
def tt_by_url():
    return get_detail(request.args.get('url', ''), 'tt')


@app.route('/tt/<anyid>')
def tt_by_id(anyid):
    return get_detail(media_url('tt', anyid), 'tt')


@app.route('/sc')
def sc_by_url():
    return get_detail(request.args.get('url', ''), 'sc')


@app.route('/sc/<user>/<track>')
def sc_detail_route(user, track):
    return get_detail(f'https://soundcloud.com/{quote(user, safe="")}/{quote(track, safe="")}', 'sc')


def begin(kind, url):
    try:
        return start_job(kind, extract(url), url)
    except Exception as exc:
        LOG.warning('Could not start download: %s', exc)
        return error_page(user_error(exc), 400 if isinstance(exc, ValueError) else 502)


@app.route('/yt/<vid>/start/<mode>')
def yt_start(vid, mode):
    if mode not in ('highest', 'hd', 'audio'):
        abort(404)
    return begin('yt-' + mode, media_url('yt', vid))


@app.route('/tt/<vid>/start/video')
def tt_start_video(vid):
    return begin('tt-video', media_url('tt', vid))


@app.route('/sc/<sid>/start/mp3')
def sc_start_mp3(sid):
    return begin('sc-mp3', media_url('sc', sid))


def attachment(kind, item_id, is_sub=False):
    try:
        info = extract(media_url(kind, item_id))
        if is_sub:
            sub = subtitles(info)
            if not sub:
                return error_page('No subtitles are available.', 404)
            url, ext, lang = sub
            name = f'{info.get("title") or "subtitles"} [{lang}]'
        else:
            url = thumb(info)
            if not url:
                return error_page('No cover image is available.', 404)
            ext = Path(urlsplit(url).path).suffix.lower().lstrip('.')
            if ext not in ('png', 'jpg', 'jpeg', 'webp'):
                ext = 'jpg'
            name = info.get('title') or 'thumbnail'
        if urlsplit(url).scheme != 'https':
            raise ValueError('Attachment URL must use HTTPS.')
        with requests.get(url, timeout=(5, 20), stream=True) as response:
            response.raise_for_status()
            data = BytesIO()
            for chunk in response.iter_content(65536):
                if data.tell() + len(chunk) > 16 * 1024 * 1024:
                    raise ValueError('Attachment is too large.')
                data.write(chunk)
            data.seek(0)
        mime = 'text/vtt' if is_sub else 'image/' + ('jpeg' if ext in ('jpg', 'jpeg') else ext)
        return send_file(data, mimetype=mime, as_attachment=True, download_name=clean_filename(name, ext))
    except Exception as exc:
        LOG.warning('Attachment error: %s', exc)
        return error_page(user_error(exc), 502)


@app.route('/yt/<vid>/thumb')
def yt_thumb(vid):
    return attachment('yt', vid)


@app.route('/yt/<vid>/subs')
def yt_subs(vid):
    return attachment('yt', vid, True)


@app.route('/tt/<vid>/thumb')
def tt_thumb(vid):
    return attachment('tt', vid)


@app.route('/sc/<sid>/cover')
def sc_cover(sid):
    return attachment('sc', sid)


def job_snapshot(jid):
    with LOCK:
        job = JOBS.get(jid)
        if not job:
            return None
        result = job.copy()
        result['queue_position'] = sum(1 for j in JOBS.values() if j['stage'] == 'queued' and j['created'] < job['created']) + 1 if job['stage'] == 'queued' else 0
    if result['path'] and not Path(result['path']).is_file():
        update_job(jid, path=None, stage='expired', error='This download has expired. Please start it again.')
        result.update(path=None, stage='expired', error='This download has expired. Please start it again.')
    return result


@app.route('/job/<jid>')
def job_view(jid):
    job = job_snapshot(jid)
    if not job:
        abort(404)
    progress = float(job['progress'] or 0)
    body = f'''<div class="card"><h1>{html.escape(job['title'])}</h1><div class="progress-card"><div style="display:flex;justify-content:space-between"><span class="small" id="stage" role="status" aria-live="polite">{html.escape(job['stage'])}</span><span class="small">Queue: <span id="qpos">{job['queue_position']}</span></span></div><div class="bar-wrap" role="progressbar" aria-label="Download progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{progress}" id="progressWrap"><div class="bar" id="bar" style="width:{progress}%"></div></div><p class="small"><span id="pct">{progress:.1f}%</span> · <span id="speed">0 B/s</span><span id="eta"></span></p><div id="done" class="btns" style="display:none"><a class="btn" id="download" href="#">Download</a><a class="btn" href="/">New download</a></div><p id="err" class="error" role="alert" style="display:none"></p></div></div>
<script>(()=>{{const jid={json.dumps(jid)},auto={str(request.args.get('own') == '1').lower()};const $=id=>document.getElementById(id);let failures=0,started=false;const fmt=v=>{{let n=v||0,i=0;while(n>=1024&&i<3){{n/=1024;i++}}return n.toFixed(1)+' '+['B/s','KB/s','MB/s','GB/s'][i]}};async function poll(){{if(document.hidden){{setTimeout(poll,2000);return}}try{{const r=await fetch('/job/'+jid+'/status',{{cache:'no-store'}});if(!r.ok)throw Error();const j=await r.json();failures=0;$('err').style.display='none';$('stage').textContent=j.stage;$('qpos').textContent=j.queue_position;$('bar').style.width=j.progress+'%';$('progressWrap').setAttribute('aria-valuenow',j.progress);$('pct').textContent=j.progress.toFixed(1)+'%';$('speed').textContent=fmt(j.speed);$('eta').textContent=j.eta==null?'':' · ETA '+Math.round(j.eta)+'s';if(j.error){{$('err').textContent=j.error;$('err').style.display='block';return}}if(j.ready&&j.file_url){{$('done').style.display='flex';$('download').href=j.file_url;if(auto&&!started){{started=true;const a=document.createElement('a');a.href=j.file_url;a.download='';document.body.append(a);a.click();a.remove()}}return}}setTimeout(poll,1200)}}catch(e){{failures++;$('err').style.display='block';$('err').textContent='Connection interrupted. Retrying…';setTimeout(poll,Math.min(10000,1000*2**Math.min(failures,4)))}}}}poll()}})();</script>'''
    return page_shell(body, 'Processing…')


@app.route('/job/<jid>/status')
def job_status(jid):
    job = job_snapshot(jid)
    if not job:
        return jsonify(error='Unknown job'), 404
    return jsonify(id=jid, stage=job['stage'], progress=round(float(job['progress'] or 0), 1), speed=job['speed'] or 0,
                   eta=job['eta'], queue_position=job['queue_position'], ready=bool(job['path']),
                   file_url=url_for('job_file', jid=jid) if job['path'] else None, error=job['error'])


@app.route('/job/<jid>/file')
def job_file(jid):
    job = job_snapshot(jid)
    if not job or not job['path']:
        abort(404)
    path = Path(job['path']).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=job['filename'], conditional=True)


@app.route('/info/')
def admin():
    try:
        local = importlib.metadata.version('yt-dlp')
    except importlib.metadata.PackageNotFoundError:
        local = 'not installed'
    try:
        response = requests.get('https://pypi.org/pypi/yt-dlp/json', timeout=(3, 5))
        response.raise_for_status()
        latest = response.json()['info']['version']
        status = 'Up to date' if latest == local else 'Update available'
    except (requests.RequestException, KeyError, ValueError):
        latest, status = 'Unavailable', 'Could not check for updates'
    return page_shell(f'<div class="card"><h1>YT-DLP status</h1><h2>{html.escape(status)}</h2><p>Installed: {html.escape(local)}<br>Latest: {html.escape(latest)}</p></div>', 'Server Status')


@app.route('/json/')
def block_json_probe():
    abort(403)


def clean_expired(now=None):
    now = now or time.time()
    cutoff = now - CACHE_HOURS * 3600
    with LOCK:
        active = {j['key'] for j in JOBS.values() if j['stage'] in ('queued', 'starting', 'downloading', 'processing')}
    removed = 0
    for path in ROOT.iterdir():
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in ('.mp3', '.mp4') or path.stat().st_mtime >= cutoff:
            continue
        if any(f' [{key.split(":", 1)[1]}] [{KINDS[key.split(":", 1)[0]][0]}]' in path.stem for key in active if key.split(':', 1)[0] in KINDS):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            LOG.exception('Could not expire %s', path.name)
    with LOCK:
        for jid, job in list(JOBS.items()):
            if job['path'] and not Path(job['path']).exists():
                job.update(path=None, stage='expired', error='This download has expired. Please start it again.')
            if job['created'] < now - 86400 and job['stage'] in ('ready', 'expired', 'error'):
                JOBS.pop(jid)
                if KEYS.get(job['key']) == jid:
                    KEYS.pop(job['key'], None)
    return removed


def cleanup_loop():
    while True:
        time.sleep(3600)
        try:
            clean_expired()
        except Exception:
            LOG.exception('Cache cleanup failed')


threading.Thread(target=cleanup_loop, daemon=True, name='downtil-cleanup').start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '80')), debug=os.getenv('FLASK_DEBUG') == '1', use_reloader=False)
