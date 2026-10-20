"""``python -m app`` starts the server through :func:`app.main.run`.

Using the module entry point (instead of calling uvicorn directly) keeps the
bind-safety check in the launch path, so a non-loopback address without an
access token is refused before the socket opens (PRD D-05).
"""

from .main import run

if __name__ == "__main__":
    run()
