"""DownTil: lightweight Flask media downloader."""
import html
import importlib.metadata
import json
import logging
import math
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
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.url_map.strict_slashes = False
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "80"))
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
        message = str(exc).lower()
        if ('tiktok.com' in message and '/photo/' in message) or 'tiktok slideshow' in message:
            return 'TikTok photo posts and slideshows are not supported. Paste a TikTok video link instead.'
        if 'unsupported url' in message or 'unsupported site' in message:
            return 'This link is not supported. Paste a direct YouTube, TikTok video, or SoundCloud track link.'
        if 'private' in message or 'members only' in message:
            return 'This media is private or restricted and cannot be downloaded.'
        if 'sign in' in message or 'login required' in message or 'cookies' in message:
            return 'This media requires a login. The server may need an updated cookies.txt file.'
        if 'not available in your country' in message or 'geo-restricted' in message:
            return 'This media is not available in the server’s region.'
        if '429' in message or 'rate limit' in message or 'too many requests' in message:
            return 'The media service is limiting requests. Try again in a little while.'
        if 'ffmpeg' in message or 'ffprobe' in message:
            return 'Media processing failed. Check that FFmpeg and ffprobe are installed on the server.'
        if 'no video formats' in message or 'requested format is not available' in message:
            return 'No downloadable media was found in the selected quality. Try another quality.'
        if 'unavailable' in message or 'removed' in message or 'deleted' in message:
            return 'This media is unavailable or has been removed.'
        return 'The media service could not process this link. It might be unavailable or temporarily unsupported.'
    if isinstance(exc, ValueError):
        return str(exc)
    return 'Something went wrong on the server. Please try again.'


def update_job(jid, **changes):
    with LOCK:
        if jid in JOBS:
            JOBS[jid].update(changes)


def download(jid, url, info, kind):
    """Report ONLY the current transfer's known progress, never an invented whole-job ETA.

    yt-dlp calls its download hook separately for video and audio streams. A
    finished stream is not a finished job; merging, tagging and conversion may
    take an unknown amount of time. Only the verified final file means 100%.
    """
    tag, _ = KINDS[kind]
    last = 0.0
    last_file = None

    def progress(data):
        nonlocal last, last_file
        state = data.get('status')
        if state == 'finished':
            last = 0.0
            update_job(jid, stage='processing', detail='File downloaded; preparing the next step…',
                       progress=None, progress_scope='Preparing output', speed=0, eta=None, estimated=False)
            return
        if state != 'downloading':
            return
        filename = data.get('filename') or data.get('tmpfilename')
        current_file = str(filename) if filename else None
        now = time.monotonic()
        if current_file == last_file and now - last < .3:
            return
        last, last_file = now, current_file
        total_exact = data.get('total_bytes')
        total_guess = data.get('total_bytes_estimate')
        total = total_exact or total_guess
        downloaded = data.get('downloaded_bytes') or 0
        raw_speed = data.get('speed') or 0
        try:
            total = float(total) if total is not None else 0.0
            downloaded = float(downloaded)
            speed = float(raw_speed)
            if not math.isfinite(total) or total <= 0:
                total = 0.0
            if not math.isfinite(downloaded) or downloaded < 0:
                downloaded = 0.0
            if not math.isfinite(speed) or speed <= 0:
                speed = 0.0
        except (TypeError, ValueError, OverflowError):
            total, downloaded, speed = 0.0, 0.0, 0.0
        percent = round(min(100.0, 100 * downloaded / total), 1) if total else None
        # ETA covers only the active file, not a second stream or FFmpeg work.
        remaining = max(0.0, total - downloaded) if total else 0.0
        eta = round(remaining / speed, 1) if total and speed else None
        guessed = not bool(total_exact)
        update_job(jid, stage='downloading', detail='Downloading current file…', progress=percent,
                   progress_scope='Approx. current file' if guessed and total else 'Current file',
                   speed=speed, eta=eta, estimated=True)

    def postprocess(data):
        if data.get('status') == 'started':
            name = str(data.get('postprocessor') or '')
            description = ('Converting audio…' if 'ExtractAudio' in name else
                           'Merging or converting media…' if 'FFmpeg' in name else
                           'Embedding artwork…' if 'Thumbnail' in name else 'Finishing output…')
            update_job(jid, stage='processing', detail=description, progress=None,
                       progress_scope='Processing', speed=0, eta=None, estimated=False)
        elif data.get('status') == 'finished':
            update_job(jid, stage='processing', detail='Finishing output…', progress=None,
                       progress_scope='Processing', speed=0, eta=None, estimated=False)

    update_job(jid, stage='starting', detail='Connecting to the media service…', progress=None,
               progress_scope='Waiting', speed=0, eta=None, estimated=False)
    opts = base_opts(format_opts(kind, info), str(ROOT / f'%(title).180B [%(id)s] [{tag}].%(ext)s'))
    opts['progress_hooks'] = [progress]
    opts['postprocessor_hooks'] = [postprocess]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(url, download=True)
        file = existing_file(str(info['id']), tag)
        if file is None:
            raise RuntimeError('Download completed but no output file was found.')
        update_job(jid, stage='processing', detail='Finishing metadata…', progress=None,
                   progress_scope='Processing', speed=0, eta=None, estimated=False)
        if kind == 'sc-mp3':
            tag_soundcloud(file, result or info)
        # 100% means that the final file really exists and any metadata work ended.
        if not file.is_file():
            raise RuntimeError('Final file disappeared before it could be served.')
        update_job(jid, stage='ready', detail='Ready to download', progress=100.0,
                   progress_scope='Complete', speed=0, eta=None, estimated=False, path=str(file))
    except Exception as exc:
        LOG.exception('Download %s failed', jid)
        update_job(jid, stage='error', detail='Download failed', progress=None,
                   progress_scope='Unavailable', speed=0, eta=None, estimated=False, error=user_error(exc))


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
                     'stage': 'ready' if file else 'queued', 'detail': 'Ready to download' if file else 'Waiting for a worker…',
                     'progress': 100.0 if file else None, 'progress_scope': 'Complete' if file else 'Waiting',
                     'speed': 0, 'eta': None, 'estimated': False, 'path': str(file) if file else None, 'error': None,
                     'filename': clean_filename(info.get('title') or 'download', ext)}
        KEYS[key] = jid
        if not file:
            POOL.submit(download, jid, url, info, kind)
    return redirect(url_for('job_view', jid=jid, own='1'))


def page_shell(body, title='DownTil', footer='Files are processed on the server. Finished downloads are cached and reused.'):
    esc = lambda value: html.escape(str(value), quote=True)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<meta name="theme-color" content="#0b0b0c">'
            f'<link rel="icon" href="{url_for("static", filename="favicon.png")}">'
            f'<link rel="stylesheet" href="{url_for("static", filename="site.css", v="3")}">'
            f'<title>{esc(title)}</title></head><body><main class="wrap">'
            f'<form class="search" action="/" method="get">'
            f'<input id="qinput" type="url" name="q" aria-label="Media URL" '
            f'placeholder="Paste YouTube / TikTok / SoundCloud URL…" value="{esc(request.args.get("q", ""))}" required>'
            f'</form>{body}'
            f'<div class="footer">{esc(footer)}</div></main></body></html>')


def error_page(message, status=400):
    banner = (f'<div class="error-banner" role="alert">'
              f'<strong>Could not process that link</strong>'
              f'<span>{html.escape(message)}</span>'
              f'<a href="/" aria-label="Dismiss error">Dismiss</a></div>')
    landing = ('<div class="card"><h1>Paste a link above</h1>'
               '<h2 class="small">Download from YouTube, TikTok, and SoundCloud.</h2></div>')
    return page_shell(banner + landing, 'DownTil'), status


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
        primary = [(f'Highest ({height}p)' if height else 'Highest quality',
                    url_for('yt_start', vid=item_id, mode='highest'))]
        primary.append(('HD (≤1080p)', url_for('yt_start', vid=item_id, mode='hd')))
        rates = [f.get('abr') or f.get('tbr') or 0 for f in info.get('formats') or []
                 if f.get('vcodec') in (None, 'none') and f.get('acodec') not in (None, 'none')]
        kbps = round(max(rates)) if rates else 0
        primary.append((f'Audio ({kbps} kbps)' if kbps else 'Audio (best)', url_for('yt_start', vid=item_id, mode='audio')))
        secondary = []
        sub = subtitles(info)
        if sub:
            secondary.append((f'Subtitles ({sub[2].upper()})', url_for('yt_subs', vid=item_id)))
        if image:
            secondary.append(('Thumbnail', url_for('yt_thumb', vid=item_id)))
        media = f'<iframe class="thumb" src="https://www.youtube.com/embed/{quote(item_id)}?rel=0" title="YouTube video player" loading="lazy" allow="picture-in-picture; encrypted-media" allowfullscreen></iframe>'
    elif kind == 'tt':
        primary = [('Download Video', url_for('tt_start_video', vid=item_id))]
        secondary = [('Thumbnail', url_for('tt_thumb', vid=item_id))] if image else []
        media = f'<img class="thumb" src="{html.escape(image or "", quote=True)}" loading="lazy" alt="Media cover">' if image else ''
    else:
        primary = [('Download MP3', url_for('sc_start_mp3', sid=item_id))]
        secondary = [('Cover', url_for('sc_cover', sid=item_id))] if image else []
        media = f'<img class="thumb" src="{html.escape(image or "", quote=True)}" loading="lazy" alt="Media cover">' if image else ''
    def controls(actions, group):
        if not actions:
            return ''
        links = ''.join(f'<a class="btn" href="{html.escape(href, quote=True)}">{html.escape(label)}</a>' for label, href in actions)
        return f'<div class="btns {group}{" single" if len(actions) == 1 else ""}">{links}</div>'
    body = (f'<div class="card"><div class="row">{media}<div class="meta"><h1>{html.escape(title)}</h1>'
            f'<h2>{html.escape(creator)}</h2>{controls(primary, "download-actions")}'
            f'{controls(secondary, "utility-actions")}</div></div></div>')
    return page_shell(body, f'{title} - {creator}')


def get_detail(url, kind):
    try:
        url, _ = validate_url(url, kind)
        if kind == 'tt' and '/photo/' in urlsplit(url).path:
            return error_page('TikTok photo posts and slideshows are not supported. Paste a TikTok video link instead.', 400)
        return detail_for(kind, extract(url))
    except Exception as exc:
        LOG.warning('Metadata lookup failed: %s', exc)
        unsupported_photo = kind == 'tt' and '/photo/' in str(exc).lower()
        return error_page(user_error(exc), 400 if isinstance(exc, ValueError) or unsupported_photo else 502)


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
        update_job(jid, path=None, stage='expired', detail='Cached file expired', progress=None,
                   error='This download has expired. Please start it again.')
        result.update(path=None, stage='expired', detail='Cached file expired', progress=None,
                      error='This download has expired. Please start it again.')
    return result


@app.route('/job/<jid>')
def job_view(jid):
    job = job_snapshot(jid)
    if not job:
        abort(404)
    value = job['progress']
    known = isinstance(value, (int, float)) and math.isfinite(value)
    progress = max(0.0, min(100.0, value)) if known else 0.0
    scope = html.escape(job['progress_scope'])
    body = (f'<div class="card"><h1>{html.escape(job["title"])}</h1>'
            f'<div class="progress-card"><div class="progress-head">'
            f'<span class="small" id="stage" role="status" aria-live="polite">{html.escape(job["detail"])}</span>'
            f'<span class="small" id="queue" {"" if job["stage"] == "queued" else "hidden"}>Queue: <span id="qpos">{job["queue_position"]}</span></span></div>'
            f'<div class="bar-wrap {"" if known else "indeterminate"}" role="progressbar" aria-label="Download progress" '
            f'aria-valuemin="0" aria-valuemax="100" {f"aria-valuenow={progress:.1f}" if known else ""} '
            f'aria-valuetext="{scope}" id="progressWrap"><div class="bar" id="bar" style="width:{progress:.1f}%"></div></div>'
            f'<p class="small progress-details"><span id="scope">{scope}</span>'
            f'<span id="pct" {"" if known else "hidden"}>{progress:.1f}%</span>'
            f'<span id="speed" hidden></span><span id="eta" hidden></span></p>'
            f'<div id="done" class="btns" hidden><a class="btn" id="download" href="#">Download</a>'
            f'<a class="btn" href="/">New download</a></div><p id="err" class="error" role="alert" hidden></p></div></div>'
            f'<script>window.DOWNTIL_JOB={json.dumps({"id": jid, "auto": request.args.get("own") == "1"})};</script>'
            f'<script src="{url_for("static", filename="job.js", v="2")}" defer></script>')
    return page_shell(body, 'Processing…')


@app.route('/job/<jid>/status')
def job_status(jid):
    job = job_snapshot(jid)
    if not job:
        return jsonify(error='Unknown job'), 404
    return jsonify(id=jid, stage=job['stage'], detail=job['detail'], progress=job['progress'],
                   progress_scope=job['progress_scope'], speed=job['speed'], eta=job['eta'],
                   estimated=job['estimated'], queue_position=job['queue_position'],
                   ready=bool(job['path'] and job['stage'] == 'ready'),
                   file_url=url_for('job_file', jid=jid) if job['path'] and job['stage'] == 'ready' else None,
                   error=job['error'])


@app.route('/job/<jid>/file')
def job_file(jid):
    job = job_snapshot(jid)
    if not job or not job['path'] or job['stage'] != 'ready':
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
                job.update(path=None, stage='expired', detail='Cached file expired', progress=None,
                           error='This download has expired. Please start it again.')
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
    app.run(host=HOST, port=PORT, debug=os.getenv('FLASK_DEBUG') == '1', use_reloader=False)
