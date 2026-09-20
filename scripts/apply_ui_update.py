"""One-time, assertion-checked patch of the existing DownTil entrypoint."""
from pathlib import Path


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'Expected exactly one match for {old[:90]!r}, found {count}')
    return text.replace(old, new, 1)


def replace_block(text: str, start: str, end: str, replacement: str) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise RuntimeError(f'Cannot locate unique block: {start!r} -> {end!r}')
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[:begin] + replacement + text[finish:]


path = Path('main.py')
source = path.read_text(encoding='utf-8')
source = replace_once(
    source,
    'from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, TPOS\n\napp = Flask(__name__)',
    'from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, TPOS\nfrom dotenv import load_dotenv\n\nload_dotenv()\n\napp = Flask(__name__)',
)
source = replace_once(
    source,
    'app.url_map.strict_slashes = False\n',
    'app.url_map.strict_slashes = False\nHOST = os.environ.get("HOST", "127.0.0.1")\nPORT = int(os.environ.get("PORT", "80"))\n',
)
source = replace_block(
    source,
    'def user_error(exc):\n',
    'def update_job(jid, **changes):\n',
    '''def user_error(exc):
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


''',
)
source = replace_once(
    source,
    'f\'<link rel="stylesheet" href="{url_for("static", filename="site.css", v="2")}">\'',
    'f\'<link rel="stylesheet" href="{url_for("static", filename="site.css", v="3")}">\'',
)
source = replace_once(
    source,
    'f\'<button class="btn" type="submit">Go</button></form>{body}\'',
    'f\'</form>{body}\'',
)
source = replace_block(
    source,
    'def error_page(message, status=400):\n',
    '@app.after_request\n',
    '''def error_page(message, status=400):
    banner = (f'<div class="error-banner" role="alert">'
              f'<strong>Could not process that link</strong>'
              f'<span>{html.escape(message)}</span>'
              f'<a href="/" aria-label="Dismiss error">Dismiss</a></div>')
    landing = ('<div class="card"><h1>Paste a link above</h1>'
               '<h2 class="small">Download from YouTube, TikTok, and SoundCloud.</h2></div>')
    return page_shell(banner + landing, 'DownTil'), status


''',
)
source = replace_once(
    source,
    "        primary = []\n        if height > 1080:\n            primary.append((f'Highest ({height}p)', url_for('yt_start', vid=item_id, mode='highest')))\n        primary.append(('HD (≤1080p)', url_for('yt_start', vid=item_id, mode='hd')))\n",
    "        primary = [(f'Highest ({height}p)' if height else 'Highest quality',\n                    url_for('yt_start', vid=item_id, mode='highest'))]\n        primary.append(('HD (≤1080p)', url_for('yt_start', vid=item_id, mode='hd')))\n",
)
source = replace_once(
    source,
    "        url, _ = validate_url(url, kind)\n        return detail_for(kind, extract(url))",
    "        url, _ = validate_url(url, kind)\n        if kind == 'tt' and '/photo/' in urlsplit(url).path:\n            return error_page('TikTok photo posts and slideshows are not supported. Paste a TikTok video link instead.', 400)\n        return detail_for(kind, extract(url))",
)
source = replace_once(
    source,
    "        return error_page(user_error(exc), 400 if isinstance(exc, ValueError) else 502)\n\n\n@app.route('/yt')",
    "        unsupported_photo = kind == 'tt' and '/photo/' in str(exc).lower()\n        return error_page(user_error(exc), 400 if isinstance(exc, ValueError) or unsupported_photo else 502)\n\n\n@app.route('/yt')",
)
source = replace_once(
    source,
    "    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '80')), debug=os.getenv('FLASK_DEBUG') == '1', use_reloader=False)",
    "    app.run(host=HOST, port=PORT, debug=os.getenv('FLASK_DEBUG') == '1', use_reloader=False)",
)
assert '<button class="btn" type="submit">Go</button>' not in source
path.write_text(source, encoding='utf-8')

test_path = Path('tests/test_progress.py')
test = test_path.read_text(encoding='utf-8')
test = replace_once(test, "    assert b'grid-template-columns:repeat(2,minmax(0,1fr))' in css.data\n", "    assert b'grid-template-columns:repeat(4,max-content)' in css.data\n")
test_path.write_text(test, encoding='utf-8')
print('Applied HOST/PORT, one-line input, friendly inline errors and compact quality buttons.')
