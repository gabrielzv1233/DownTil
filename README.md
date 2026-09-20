# DownTil

A lightweight Flask downloader for individual YouTube, TikTok videos, and SoundCloud tracks. It keeps the compact dark UI, the `/?q=...` shortcut-compatible URL entrypoint, audio/video quality choices, thumbnails, subtitles, SoundCloud MP3 tags, and a server-side cache.

## Run

Install [uv](https://docs.astral.sh/uv/) and [FFmpeg](https://ffmpeg.org/download.html). Ensure `ffmpeg` and `ffprobe` are on `PATH`.

```bash
git clone -b dev https://github.com/gabrielzv1233/DownTil.git
cd DownTil
uv sync
uv run python main.py
```

Alternatively, use `pip install -r requirements.txt` and `python main.py`. You can continue to use `uv run python pterodactyl.py` with the same environment settings. Both entrypoints support an optional `.env` file via `python-dotenv` and respect actual environment variables over `.env`.

## HOST and PORT

Both entrypoints use exactly these variables:

```python
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "80"))
```

For **Pterodactyl or Docker**, set `HOST=0.0.0.0` and set `PORT` to the server's allocated port (for example `PORT=8080`). `127.0.0.1` only accepts connections inside the container. Do not set `HOST` to the node's public IP, which usually is not bound within the container. The legacy `INTERNAL_IP` and `SERVER_PORT` variables are not used; map the allocated port to `PORT` in your egg or environment. If you have a reverse proxy, forward it to the same allocated port.

Copy `.env.example` to `.env` to configure this outside Pterodactyl, or supply the variables in the panel. Additional settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOWNLOAD_DIR` | `./downloads` | Cached downloads |
| `COOKIES_FILE` | `./cookies.txt` | Optional yt-dlp cookies |
| `MAX_WORKERS` | up to 4 depending on CPU | Concurrent downloads, capped at 8 |
| `CACHE_HOURS` | `6` | Completed file retention |
| `FLASK_DEBUG` | `0` | Development only, do not enable publicly |

The URL input submits with Enter. Unsupported links produce a concise error banner rather than a separate error page. TikTok photo posts and slideshows are not currently supported. yt-dlp and FFmpeg should be kept updated as media services change.

Downloads are cached across restarts, but running job state is in memory. Run one application process unless the queue is moved to a shared backing store. Do not commit cookies. For internet-facing production deployments, use a reverse proxy and a production WSGI server. Download only media you have permission to use.

## Tests

```bash
uv run pytest -q
```

Offline tests cover validation, download progress, job handling, the URL UI, and environment configuration. The previous implementation is retained in `legacy.py` for reference, not as the production entrypoint.
