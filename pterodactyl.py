"""Compatibility launcher using the same HOST and PORT configuration as main.py."""
import os

from dotenv import load_dotenv

load_dotenv(override=False)

from main import app  # noqa: E402

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "80"))

if __name__ == '__main__':
    if not 1 <= PORT <= 65535:
        raise ValueError('PORT must be between 1 and 65535.')
    app.run(host=HOST, port=PORT, debug=os.environ.get('FLASK_DEBUG') == '1', use_reloader=False)
