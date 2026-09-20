"""Offline regression tests for the download hooks and responsive controls."""
import main


def test_progress_tracks_current_file_and_only_finishes_with_output(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    jid = 'hook-test'
    with main.LOCK:
        main.JOBS[jid] = {
            'id': jid, 'key': 'yt-hd:clip', 'title': 'Demo', 'created': 1.0,
            'stage': 'queued', 'detail': 'Waiting', 'progress': None,
            'progress_scope': 'Waiting', 'speed': 0, 'eta': None,
            'estimated': False, 'path': None, 'error': None, 'filename': 'Demo.mp4',
        }
    snapshots = []
    original = main.update_job

    def record(jid, **changes):
        original(jid, **changes)
        snapshots.append(main.JOBS[jid].copy())

    monkeypatch.setattr(main, 'update_job', record)

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            progress = self.opts['progress_hooks'][0]
            postprocess = self.opts['postprocessor_hooks'][0]
            progress({'status': 'downloading', 'filename': 'video.f137.mp4',
                      'total_bytes': 100, 'downloaded_bytes': 25, 'speed': 25, 'eta': 9999})
            progress({'status': 'finished', 'filename': 'video.f137.mp4'})
            postprocess({'status': 'started', 'postprocessor': 'FFmpegMerger'})
            progress({'status': 'downloading', 'filename': 'audio.f140.m4a',
                      'total_bytes_estimate': 200, 'downloaded_bytes': 50, 'speed': 50})
            progress({'status': 'downloading', 'filename': 'unknown.part',
                      'downloaded_bytes': 30, 'speed': 10, 'eta': 0})
            postprocess({'status': 'finished', 'postprocessor': 'FFmpegMerger'})
            (tmp_path / 'Demo [clip] [hd].mp4').write_bytes(b'completed')
            return {'id': 'clip', 'title': 'Demo'}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', FakeYDL)
    main.download(jid, 'https://youtu.be/clip', {'id': 'clip', 'title': 'Demo', 'formats': []}, 'yt-hd')

    first = next(state for state in snapshots if state['stage'] == 'downloading')
    assert first['progress'] == 25
    assert first['eta'] == 3  # Not the bogus ETA field in the fake hook.
    assert first['progress_scope'] == 'Current file'
    processing = [state for state in snapshots if state['stage'] == 'processing']
    assert processing and all(state['progress'] is None and state['eta'] is None for state in processing)
    estimated = next(state for state in snapshots if state['progress_scope'] == 'Approx. current file')
    assert estimated['progress'] == 25 and estimated['eta'] == 3 and estimated['estimated']
    unknown = next(state for state in snapshots if state['stage'] == 'downloading' and state['progress'] is None)
    assert unknown['eta'] is None
    assert all(state['progress'] != 100 for state in snapshots[:-1])
    assert snapshots[-1]['stage'] == 'ready' and snapshots[-1]['progress'] == 100
    assert main.app.test_client().get(f'/job/{jid}/status').json['ready']


def test_no_output_never_reports_completion(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    jid = 'no-output'
    with main.LOCK:
        main.JOBS[jid] = {'stage': 'queued', 'progress': None, 'path': None, 'error': None}

    class NoFileYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def extract_info(self, url, download=False):
            self.opts['progress_hooks'][0]({'status': 'finished'})
            return {'id': 'missing'}

    monkeypatch.setattr(main.yt_dlp, 'YoutubeDL', NoFileYDL)
    main.download(jid, 'https://youtu.be/missing', {'id': 'missing'}, 'yt-hd')
    assert main.JOBS[jid]['stage'] == 'error'
    assert main.JOBS[jid]['progress'] is None
    assert main.JOBS[jid]['path'] is None


def test_action_layout_and_progress_page(monkeypatch):
    info = {'id': 'abc', 'title': 'Test', 'uploader': 'Demo',
            'thumbnail': 'https://example.com/thumbnail.jpg',
            'formats': [{'height': 1080, 'vcodec': 'avc1', 'acodec': 'none'},
                        {'abr': 132, 'vcodec': 'none', 'acodec': 'mp4a'}],
            'subtitles': {'en': [{'url': 'https://example.com/caption.vtt', 'ext': 'vtt'}]}}
    with main.app.test_request_context('/'):
        markup = main.detail_for('yt', info)
    assert 'class="btns download-actions"' in markup
    assert 'class="btns utility-actions"' in markup
    assert 'Audio (132 kbps)' in markup
    client = main.app.test_client()
    css = client.get('/static/site.css')
    assert css.status_code == 200
    assert b'grid-template-columns:repeat(4,max-content)' in css.data
    assert client.get('/static/job.js').status_code == 200
