# DownTil

A lightweight, server-rendered downloader for individual YouTube, TikTok, and SoundCloud videos and tracks. The dark UI, link-sharing route (`/?q=...`), video/audio choices, thumbnails, subtitles, and SoundCloud MP3 tags are preserved.

## Run with uv

1. Install [uv](https://docs.astral.sh/uv/) and [FFmpeg](https://ffmpeg.org/download.html). Make sure `ffmpeg` and `ffprobe` are available on `PATH`.
2. Clone the repository and run `uv sync`.
3. Start with `uv run python main.py` and visit `http://localhost:80/` (or the port in `PORT`). On Linux, use `PORT=8080` if you do not have permission to bind port 80.

You can also use `python -m venv .venv`, install with `pip install -r requirements.txt`, then run `python main.py`.

### Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `80` | HTTP port |
| `DOWNLOAD_DIR` | `./downloads` | Cached downloaded files |
| `COOKIES_FILE` | `./cookies.txt` | Optional yt-dlp cookies file |
| `MAX_WORKERS` | up to 4, based on CPU | Concurrent downloads, capped at 8 |
| `CACHE_HOURS` | `6` | Completed file retention |
| `FLASK_DEBUG` | `0` | Local development only, never enable on a public deployment |

Never commit `cookies.txt`. Downloads are preserved across restarts, expire by age, and jobs are held in memory (so unfinished downloads do not survive a restart). Configure a single application process because the queue is in memory. For internet-facing production hosting, run behind a reverse proxy and a production WSGI server. Keep yt-dlp and FFmpeg updated as supported websites change. Only download content you are permitted to use.

## Development

`uv run pytest -q` runs the offline route, validation, job lifecycle, and cache tests. The original code is kept in `legacy.py` on `dev` for reference. Do not run it as the production entrypoint.
