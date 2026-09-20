"""Start DownTil using Pterodactyl's host and allocated port variables."""
import os

from dotenv import load_dotenv

load_dotenv(override=False)

from main import app  # noqa: E402  (load .env before importing app configuration)


if __name__ == '__main__':
    host = os.environ.get('INTERNAL_IP') or '0.0.0.0'
    port = int(os.environ.get('SERVER_PORT') or os.environ.get('PORT') or '80')
    if not 1 <= port <= 65535:
        raise ValueError('SERVER_PORT must be between 1 and 65535.')
    app.run(host=host, port=port, debug=os.environ.get('FLASK_DEBUG') == '1', use_reloader=False)
