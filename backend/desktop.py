#!/usr/bin/env python3
"""Desktop launcher and PyInstaller frozen runtime entrypoint for Uniter."""

import argparse
import os
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    # Frozen: templates are bundled under backend/templates inside _MEIPASS.
    _bundle_templates = Path(sys._MEIPASS) / "backend" / "templates"
    _template_dir = _bundle_templates if _bundle_templates.is_dir() else Path(sys._MEIPASS) / "templates"
else:
    _template_dir = Path(__file__).resolve().parent / "templates"

import uvicorn
from backend.main import app


def get_bundle_dir() -> Path:
    """Return the base bundle directory, respecting PyInstaller sys._MEIPASS when frozen."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def open_browser(url: str, delay: float = 1.5) -> threading.Thread:
    """Open default browser to url after delay seconds in a daemon thread."""

    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    thread = threading.Thread(target=_open, daemon=True)
    thread.start()
    return thread


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="uniter",
        description="Uniter — Mobile Shop Charging & Units Management",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=56123,
        help="Port to bind (default: 56123)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        default=False,
        help="Skip opening the system default browser on launch",
    )
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> int:
    parsed = parse_args(args)

    app_url = f"http://{parsed.host}:{parsed.port}"
    if parsed.host in ("0.0.0.0", ""):
        app_url = f"http://127.0.0.1:{parsed.port}"

    if not parsed.no_browser:
        open_browser(app_url)

    import uvicorn

    config = uvicorn.Config(app, host=parsed.host, port=parsed.port, log_level="info")
    server = uvicorn.Server(config)

    try:
        server.run()
    finally:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
