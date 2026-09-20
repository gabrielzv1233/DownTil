"""Regression coverage for the compact grayscale UI and friendly error banners."""
import yt_dlp

import main


def test_search_is_one_line_and_does_not_render_go_button():
    client = main.app.test_client()
    response = client.get('/')
    assert response.status_code == 200
    assert b'<input id="qinput" type="url"' in response.data
    assert b'<button class="btn" type="submit">Go</button>' not in response.data
    css = client.get('/static/site.css').data
    assert b'white-space:nowrap' in css
    assert b'grid-template-columns:repeat(4,max-content)' in css
    assert b'linear-gradient' not in css
    assert b'.error-banner' in css


def test_tiktok_photo_post_is_an_inline_error_not_raw_url(monkeypatch):
    monkeypatch.setattr(main, 'extract', lambda url: (_ for _ in ()).throw(AssertionError('Should not extract a photo post')))
    response = main.app.test_client().get('/tt?url=https://www.tiktok.com/@test/photo/7559786364691139870?utm_source=whatever')
    assert response.status_code == 400
    assert b'class="error-banner"' in response.data
    assert b'TikTok photo posts and slideshows are not supported.' in response.data
    assert b'Unsupported URL:' not in response.data
    assert b'7559786364691139870' not in response.data
    assert b'Paste a link above' in response.data


def test_short_tiktok_photo_redirect_reports_friendly_error(monkeypatch):
    def no_extraction(url):
        raise yt_dlp.utils.DownloadError('Unsupported URL: https://www.tiktok.com/@someone/photo/7559786364691139870?utm_campaign=longtracking')

    monkeypatch.setattr(main, 'extract', no_extraction)
    response = main.app.test_client().get('/tt?url=https://vt.tiktok.com/abcdef/')
    assert response.status_code == 400
    assert b'TikTok photo posts and slideshows are not supported.' in response.data
    assert b'utm_campaign' not in response.data
    assert b'class="error-banner"' in response.data


def test_every_youtube_download_button_remains_available():
    info = {'id': 'testvideo', 'title': 'Test', 'uploader': 'Creator',
            'thumbnail': 'https://example.com/thumbnail.jpg',
            'formats': [{'height': 1080, 'vcodec': 'avc1', 'acodec': 'none'},
                        {'abr': 132, 'vcodec': 'none', 'acodec': 'mp4a'}],
            'subtitles': {'en': [{'url': 'https://example.com/subs.vtt', 'ext': 'vtt'}]}}
    with main.app.test_request_context('/'):
        page = main.detail_for('yt', info)
    assert 'Highest (1080p)' in page
    assert 'HD (≤1080p)' in page
    assert 'Audio (132 kbps)' in page
    assert 'Subtitles (EN)' in page
    assert 'Thumbnail' in page
    assert 'class="btns download-actions"' in page
    assert 'class="btns utility-actions"' in page


def test_invalid_url_stays_on_page_in_banner():
    response = main.app.test_client().get('/?q=http://127.0.0.1/private')
    assert response.status_code == 400
    assert b'class="error-banner"' in response.data
    assert b'Only YouTube' in response.data
    assert b'Could not process that link</h1>' not in response.data
