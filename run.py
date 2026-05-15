"""
Local dev entry point.

For PRODUCTION, use gunicorn via docker-compose; see DEPLOY.md.

This script exists purely to make `python run.py` work for local development:
it sets FLASK_ENV=development (which disables the strict SECRET_KEY check and
the Secure-cookie requirement so plain HTTP works), then starts Flask's
built-in dev server with auto-reload.
"""
import os
import webbrowser
from threading import Timer

os.environ.setdefault("FLASK_ENV", "development")
os.environ.setdefault("SECRET_KEY", "dev-only-do-not-use-in-production")

from app import create_app  # noqa: E402

app = create_app()


def _open_browser():
    webbrowser.open_new("http://127.0.0.1:5000")


if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        Timer(1.2, _open_browser).start()
    app.run(host="127.0.0.1", port=5000, debug=True)
