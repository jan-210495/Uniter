#!/usr/bin/env python3
"""Desktop launcher and PyInstaller frozen runtime entrypoint for Uniter.

Starts the Uvicorn web server, opens the browser, shows a system tray icon
(with Open / Health Check / Exit), and guards against a duplicate instance.
"""
import argparse
import io
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path

# PyInstaller windowed builds set sys.stdout/sys.stderr to None; uvicorn's
# logging config calls .isatty() on them, crashing on launch. Stub them out.
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

logger = logging.getLogger("uniter.desktop")


# ---------------------------------------------------------------------------
# Data directory (user data: SQLite DB lives here, preserved across updates).
# Set UNITER_DB *before* importing backend modules so the async engine is
# created against the per-user data directory (mirrors friend-co's
# DATABASE_PATH pattern).
# ---------------------------------------------------------------------------

def get_default_data_dir() -> Path:
    """Return OS-appropriate data directory: %APPDATA%/uniter/data on Windows,
    ~/.uniter/data elsewhere."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            base = Path(appdata)
        else:
            base = Path.home() / "AppData" / "Roaming"
        return base / "uniter" / "data"
    return Path.home() / ".uniter" / "data"


def ensure_data_dir(db_path: Path | None = None) -> Path:
    """Ensure parent directory for the database exists prior to connection."""
    resolved_path = Path(db_path) if db_path else get_default_data_dir() / "uniter.db"
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    if "UNITER_DB" not in os.environ:
        os.environ["UNITER_DB"] = str(resolved_path)
    return resolved_path


# Resolve and pin the data directory before importing backend modules.
_data_dir = ensure_data_dir()

import uvicorn
from backend.main import app, templates
from backend.database import Base, engine, SessionLocal
from backend.migrations import migrate_transaction_lifecycle
from backend.seed import seed_data


# ---------------------------------------------------------------------------
# Database seeding (runs once on first launch; safe to re-run)
# ---------------------------------------------------------------------------

async def _seed_database() -> None:
    """Create all tables, upgrade legacy schemas and seed data idempotently."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await migrate_transaction_lifecycle(engine)
    async with SessionLocal() as session:
        await seed_data(session)


def ensure_default_data() -> None:
    """Seed the database so the app works out of the box on first launch."""
    import asyncio

    try:
        asyncio.run(_seed_database())
    except Exception:
        logger.exception("Database seeding failed; the app may still work if tables already exist")


# ---------------------------------------------------------------------------
# System tray (pystray + Pillow, degrades gracefully when unavailable)
# ---------------------------------------------------------------------------

try:  # Optional dependency: only required for the GUI tray icon.
    import pystray
except Exception:  # pragma: no cover - environment dependent
    pystray = None

try:  # Pillow backs pystray icon images.
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover - environment dependent
    Image = None
    ImageDraw = None

#: True when the tray icon stack (pystray + Pillow) is importable.
PYSTRAY_AVAILABLE = pystray is not None and Image is not None and ImageDraw is not None


def is_gui_available() -> bool:
    """Return True when a windowing environment is likely available."""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def build_icon_image(size: int = 64):
    """Generate the Uniter tray icon: a blue badge with a white "U"."""
    if Image is None or ImageDraw is None:
        return None
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = size // 16
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=max(2, size // 5),
        fill=(30, 58, 138, 255),  # Uniter blue (blue-900)
    )
    # Block "U" monogram drawn from rectangles so no font files are needed.
    stroke = max(2, size // 9)
    left = size * 4 // 16
    right = size * 12 // 16
    top = size * 4 // 16
    bottom = size * 12 // 16
    white = (255, 255, 255, 255)
    draw.rectangle([left, top, left + stroke, bottom], fill=white)
    draw.rectangle([right - stroke, top, right, bottom], fill=white)
    draw.rectangle([left + stroke, bottom - stroke, right - stroke, bottom], fill=white)
    return image


# ---------------------------------------------------------------------------
# Single-instance guard
# ---------------------------------------------------------------------------

MUTEX_NAME = r"Local\UniterSingleInstanceMutex"
LOCK_FILE_NAME = "uniter.lock"
ERROR_ALREADY_EXISTS = 183


class SingleInstanceGuard:
    """Cross-platform single-instance guard for the Uniter desktop app."""

    def __init__(self, lock_path: str | Path | None = None, mutex_name: str = MUTEX_NAME):
        self._lock_path = Path(lock_path) if lock_path is not None else None
        self._mutex_name = mutex_name
        self._handle = None
        self._kernel32 = None
        self._lock_file = None
        self._exclusive_file = False
        self.acquired = False

    @property
    def lock_path(self) -> Path:
        return self._lock_path if self._lock_path is not None else self._default_lock_path()

    def _default_lock_path(self) -> Path:
        return get_default_data_dir() / LOCK_FILE_NAME

    def acquire(self) -> bool:
        if self.acquired:
            return True
        if sys.platform == "win32":
            self.acquired = self._acquire_mutex()
        else:
            self.acquired = self._acquire_file_lock()
        return self.acquired

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
            self._kernel32 = None
        if self._lock_file is not None:
            try:
                if not self._exclusive_file:
                    import fcntl

                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._lock_file.close()
            except Exception:
                pass
            if self._exclusive_file:
                try:
                    self.lock_path.unlink(missing_ok=True)
                except Exception:
                    pass
            self._lock_file = None
            self._exclusive_file = False
        self.acquired = False

    def _acquire_mutex(self) -> bool:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = None
        try:
            handle = kernel32.CreateMutexW(None, False, self._mutex_name)
        except Exception:
            return False
        if not handle:
            return False
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
            return False
        self._handle = handle
        self._kernel32 = kernel32
        return True

    def _acquire_file_lock(self) -> bool:
        try:
            import fcntl
        except ImportError:
            return self._acquire_lock_file_exclusive()
        path = self.lock_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(path, "a+", encoding="utf-8")
        except Exception:
            return False
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return False
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()}\n")
        lock_file.flush()
        self._lock_file = lock_file
        return True

    def _acquire_lock_file_exclusive(self) -> bool:
        path = self.lock_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            return False
        for attempt in range(2):
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 0 and self._remove_stale_lock(path):
                    continue
                return False
            except Exception:
                return False
        lock_file = os.fdopen(fd, "w", encoding="utf-8")
        lock_file.write(f"pid={os.getpid()}\n")
        lock_file.flush()
        self._lock_file = lock_file
        self._exclusive_file = True
        return True

    @staticmethod
    def _remove_stale_lock(path: Path) -> bool:
        try:
            content = path.read_text(encoding="utf-8")
            pid = int(content.split("pid=", 1)[1].split()[0])
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
        except Exception:
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Tray launcher
# ---------------------------------------------------------------------------

DEFAULT_APP_URL = "http://127.0.0.1:56123"

MENU_OPEN_LABEL = "Open Uniter"
MENU_HEALTH_LABEL = "Status / Health Check"
MENU_EXIT_LABEL = "Exit App"


class TrayLauncher:
    """Owns the pystray icon and its background thread."""

    def __init__(self, url: str = DEFAULT_APP_URL, on_exit=None, title: str = "Uniter"):
        self.url = url
        self.health_url = f"{url.rstrip('/')}/api/health"
        self.on_exit = on_exit
        self.title = title
        self._icon = None
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return PYSTRAY_AVAILABLE and is_gui_available()

    def unavailable_reason(self) -> str | None:
        if not PYSTRAY_AVAILABLE:
            return "pystray/Pillow are not installed"
        if not is_gui_available():
            return "no GUI environment detected (headless session)"
        return None

    def open_browser(self, icon=None, item=None) -> bool:
        try:
            webbrowser.open(self.url)
            logger.info("Opened %s in the default browser", self.url)
            return True
        except Exception:
            logger.exception("Could not open %s in the default browser", self.url)
            return False

    def check_health(self, timeout: float = 2.0) -> dict:
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(self.health_url, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
                status_code = getattr(response, "status", None) or response.getcode()
                ok = status_code is not None and 200 <= status_code < 300
                return {"ok": ok, "status_code": status_code, "body": body}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "status_code": exc.code, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "status_code": None, "error": str(exc)}

    def log_health_status(self, icon=None, item=None) -> dict:
        result = self.check_health()
        if result.get("ok"):
            logger.info("Uniter health check OK: HTTP %s from %s", result.get("status_code"), self.health_url)
        else:
            detail = result.get("error") or f"HTTP {result.get('status_code')}"
            logger.warning("Uniter health check FAILED for %s: %s", self.health_url, detail)
        return result

    def request_exit(self, icon=None, item=None) -> None:
        logger.info("Exit requested from the system tray; shutting down Uniter")
        if self.on_exit is not None:
            try:
                self.on_exit()
            except Exception:
                logger.exception("on_exit callback raised during tray exit")
        target = icon if icon is not None else self._icon
        if target is not None:
            try:
                target.stop()
            except Exception:
                logger.exception("Failed to stop the tray icon cleanly")

    def _build_icon(self):
        image = build_icon_image()
        menu = pystray.Menu(  # type: ignore[attr-defined]
            pystray.MenuItem(MENU_OPEN_LABEL, self.open_browser, default=True),  # type: ignore[attr-defined]
            pystray.MenuItem(MENU_HEALTH_LABEL, self.log_health_status),  # type: ignore[attr-defined]
            pystray.Menu.SEPARATOR,  # type: ignore[attr-defined]
            pystray.MenuItem(MENU_EXIT_LABEL, self.request_exit),  # type: ignore[attr-defined]
        )
        return pystray.Icon("uniter", image, self.title, menu)  # type: ignore[attr-defined]

    def start(self) -> threading.Thread | None:
        """Start the tray icon loop in a daemon thread."""
        if not self.available:
            logger.info(
                "System tray disabled (%s); continuing without tray icon",
                self.unavailable_reason(),
            )
            return None
        if self._thread is not None and self._thread.is_alive():
            return self._thread

        def _run() -> None:
            try:
                self._icon = self._build_icon()
                self._icon.run()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("System tray loop terminated with an error")
            finally:
                self._icon = None

        self._thread = threading.Thread(target=_run, name="uniter-tray", daemon=True)
        self._thread.start()
        logger.info("System tray icon started (%s)", self.url)
        return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the tray icon loop and join its thread. Never raises."""
        icon = self._icon
        if icon is not None:
            try:
                icon.stop()  # type: ignore[attr-defined]
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._icon = None
        self._thread = None


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

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
    parser.add_argument(
        "--no-tray",
        action="store_true",
        default=False,
        help="Disable the system tray icon (headless/server mode)",
    )
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> int:
    parsed = parse_args(args)

    # Seed the database so the app works out of the box on first launch.
    ensure_data_dir()
    ensure_default_data()

    target_host = "127.0.0.1" if parsed.host in ("0.0.0.0", "") else parsed.host
    app_url = f"http://{target_host}:{parsed.port}"

    guard = SingleInstanceGuard()
    if not guard.acquire():
        logger.warning("Another Uniter instance is already running; opening %s", app_url)
        webbrowser.open(app_url)
        return 0

    server = None
    tray = None
    try:
        if is_port_in_use(target_host, parsed.port):
            logger.error("Port %s is already in use", parsed.port)
            return 1

        if not parsed.no_browser:
            open_browser(app_url)

        import uvicorn

        config = uvicorn.Config(app, host=parsed.host, port=parsed.port, log_level="info")
        server = uvicorn.Server(config)

        tray = None
        if not parsed.no_tray:
            tray = TrayLauncher(url=app_url, on_exit=lambda: request_server_stop(server))
            tray.start()

        try:
            server.run()
        finally:
            if tray is not None:
                tray.stop()
        return 0
    finally:
        guard.release()


def request_server_stop(server) -> None:
    """Signal a running uvicorn Server to shut down gracefully."""
    if server is not None:
        server.should_exit = True


def is_port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True if a process is already accepting TCP connections on host:port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


if __name__ == "__main__":
    sys.exit(main())
