import os
import time

import pytest

import main


@pytest.mark.parametrize("url,kind", [
    ("https://youtu.be/abc123", "yt"),
    ("https://www.youtube.com/watch?v=abc123", "yt"),
    ("https://vt.tiktok.com/abc123", "tt"),
    ("https://soundcloud.com/user/song", "sc"),
    ("www.youtube.com/watch?v=abc123", "yt"),
])
def test_urls(url, kind):
    assert main.validate_url(url)[1] == kind


@pytest.mark.parametrize("url", [
    "http://youtube.com.evil.test/video", "http://127.0.0.1/",
    "file:///etc/passwd", "https://user:pass@youtube.com/a",
    "https://youtube.com:444/a", "https://example.com/",
    "https://www.youtube.com/a\nInjected: header",
])
def test_reject_urls(url):
    with pytest.raises(ValueError):
        main.validate_url(url)


def test_home_and_invalid_link():
    client = main.app.test_client()
    assert b"Paste a link above" in client.get("/").data
    response = client.get("/?q=http://127.0.0.1/private")
    assert response.status_code == 400
    assert b"Only YouTube" in response.data
    assert client.get("/?q=https://youtu.be/abc").status_code == 302


def test_metadata_cache(monkeypatch):
    calls = []
    class FakeYDL:
        def __init__(self, opts):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            calls.append(url)
            return {"id": "abc", "title": "Title"}
    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    with main.LOCK:
        main.METADATA.clear()
    assert main.extract("https://youtu.be/abc")["id"] == "abc"
    assert main.extract("https://youtu.be/abc")["id"] == "abc"
    assert len(calls) == 1


def test_job_dedup_and_status(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main.POOL, "submit", lambda *args: None)
    monkeypatch.setattr(main, "extract", lambda url: {"id": "test123", "title": "Demo", "formats": []})
    with main.LOCK:
        main.JOBS.clear()
        main.KEYS.clear()
    client = main.app.test_client()
    first = client.get("/yt/test123/start/audio")
    second = client.get("/yt/test123/start/audio")
    assert first.status_code == second.status_code == 302
    assert first.location.split("?")[0] == second.location.split("?")[0]
    jid = first.location.split("/job/")[1].split("?")[0]
    status = client.get(f"/job/{jid}/status")
    assert status.json["stage"] == "queued"
    assert status.json["queue_position"] == 1
    assert not status.json["ready"]
    assert client.get("/job/not-a-real-job/status").status_code == 404


def test_cache_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "CACHE_HOURS", 1)
    old = tmp_path / "Old [oldid] [mp3].mp3"
    fresh = tmp_path / "Fresh [newid] [mp3].mp3"
    old.write_bytes(b"old")
    fresh.write_bytes(b"new")
    past = time.time() - 7200
    os.utime(old, (past, past))
    assert main.clean_expired() == 1
    assert not old.exists()
    assert fresh.exists()


def test_filename_sanitization():
    assert main.clean_filename('a/b:<>?*', "mp3") == "a_b_____.mp3"
