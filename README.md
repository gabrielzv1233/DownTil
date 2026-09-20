# DownTil

A lightweight, server-rendered downloader for individual YouTube, TikTok, and SoundCloud videos and tracks. The dark UI, link-sharing route (`/?q=...`), video/audio choices, thumbnails, subtitles, and SoundCloud MP3 tags are preserved.

## Run with uv

1. Install [uv](https://docs.astral.sh/uv/) and [FFmpeg](https://ffmpeg.org/download.html). Make sure `ffmpeg` and `ffprobe` are available on `PATH`.
2. Clone the repository and run `uv sync`.
3. Start with `uv run python pterodactyl.py` and visit the host/port configured below. For local development, `uv run python main.py` is also available and still uses `PORT` (default `80`).

You can also use `python -m venv .venv`, install with `pip install -r requirements.txt`, then run `python pterodactyl.py`.

## Pterodactyl

Use this startup command in your Python egg:

```bash
uv run python pterodactyl.py
```

Or use `python pterodactyl.py` if the egg installs `requirements.txt` into its Python environment. The launcher reads Pterodactyl's `SERVER_PORT` allocation and `INTERNAL_IP` binding address. `INTERNAL_IP` defaults to `0.0.0.0` so the app is reachable outside the container. Do **not** bind the app to the node's public `SERVER_IP`; it might not exist inside the container. It uses `PORT` only as a fallback when `SERVER_PORT` is absent.

To run outside the panel, optionally copy `.env.example` to `.env`, adjust `SERVER_PORT` to your desired port, and run the same command. `python-dotenv` loads `.env` without overriding environment variables supplied by Pterodactyl. `.env` and `cookies.txt` are ignored by Git. When using the panel, ensure its primary allocation matches `SERVER_PORT`; you do not need another port for each download. Configure your external reverse proxy to point at the allocated port.

### Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `INTERNAL_IP` | `0.0.0.0` | Bind address for `pterodactyl.py` |
| `SERVER_PORT` | `PORT`, then `80` | Pterodactyl's allocated listening port for `pterodactyl.py` |
| `PORT` | `80` | Legacy/local `main.py` port; fallback for the Pterodactyl launcher |
| `DOWNLOAD_DIR` | `./downloads` | Cached downloaded files |
| `COOKIES_FILE` | `./cookies.txt` | Optional yt-dlp cookies file |
| `MAX_WORKERS` | up to 4, based on CPU | Concurrent downloads, capped at 8 |
| `CACHE_HOURS` | `6` | Completed file retention |
| `FLASK_DEBUG` | `0` | Local development only; never enable on a public deployment |

Never commit `cookies.txt`. Downloads are preserved across restarts, expire by age, and jobs are held in memory (so unfinished downloads do not survive a restart). Configure a single application process because the queue is in memory. For internet-facing production hosting, run behind a reverse proxy and a production WSGI server. Keep yt-dlp and FFmpeg updated as supported websites change. Only download content you are permitted to use.

## Development

`uv run pytest -q` runs the offline route, validation, job lifecycle, and cache tests. The original code is kept in `legacy.py` on `dev` for reference. Do not run it as the production entrypoint.
