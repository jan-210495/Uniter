"""Desktop contracts, with no display, sockets, user DB, or native tray required."""
import ast
import builtins
import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent


class FakeMenu(tuple):
    SEPARATOR = object()

    def __new__(cls, *items):
        return super().__new__(cls, items)


class FakeItem:
    def __init__(self, text, action, default=False):
        self.text, self.action, self.default = text, action, default


class FakeIcon:
    """A backend whose default action deliberately does NOT dispatch its menu."""
    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.visible = False
        self.stopped = threading.Event()
        self.stop = Mock(side_effect=self.stopped.set)
        self.base_activation = Mock()

    def __call__(self):
        self.base_activation()

    def run(self, setup):
        setup(self)
        if not self.stopped.wait(3):
            raise RuntimeError("test backend was not stopped")


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    # Import only at fixture execution: collection must not create ~/.uniter or
    # bind SQLAlchemy to a real DB. Stub backend dependencies even if the full
    # suite already imported them; monkeypatch restores the original modules.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("UNITER_DB", str(tmp_path / "unused.db"))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    dependencies = {
        "backend.main": {"app": object(), "templates": object()},
        "backend.database": {name: Mock() for name in (
            "Base", "engine", "SessionLocal", "migrate_operator_credit_refunds",
            "validate_existing_refund_schema")},
        "backend.migrations": {"SchemaUpgradeError": type("SchemaUpgradeError", (Exception,), {}),
                               "migrate_transaction_lifecycle": Mock()},
        "backend.seed": {"seed_data": Mock()},
        "uvicorn": {"Config": Mock(), "Server": Mock()},
        "pystray": {"Icon": FakeIcon, "Menu": FakeMenu, "MenuItem": FakeItem},
    }
    for name, attrs in dependencies.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("_isolated_desktop", ROOT / "backend/desktop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # A local sys proxy exercises Windows branches without confusing pytest,
    # pathlib, or unrelated tests into believing the host OS changed.
    module.sys = SimpleNamespace(platform="win32", stdout=sys.stdout, stderr=sys.stderr)
    module.uvicorn.Server.return_value.should_exit = False
    module.uvicorn.Server.return_value.force_exit = False
    module.ensure_default_data = Mock()
    module.is_port_in_use = Mock(return_value=False)
    module.webbrowser = SimpleNamespace(open=Mock(return_value=True))
    module.open_browser = Mock()
    module.SingleInstanceGuard = Mock(return_value=SimpleNamespace(
        acquire=Mock(return_value=True), release=Mock()))
    yield module
    assert not (tmp_path / "unused.db").exists()


def test_default_bind_and_flags(desktop):
    args = desktop.parse_args([])
    assert (args.host, args.port) == ("0.0.0.0", 56123)
    assert not args.no_tray and not args.no_browser
    args = desktop.parse_args(["--host", "192.168.1.4", "--port", "8123", "--no-tray", "--no-browser"])
    assert (args.host, args.port, args.no_tray, args.no_browser) == ("192.168.1.4", 8123, True, True)


@pytest.mark.parametrize("host, browser_host", [
    ("0.0.0.0", "127.0.0.1"), ("::", "[::1]"),
    ("127.0.0.1", "127.0.0.1"), ("192.168.1.4", "192.168.1.4"),
    ("localhost", "localhost"), ("2001:db8::1", "[2001:db8::1]"),
])
def test_main_preserves_bind_and_uses_local_browser_url(desktop, host, browser_host):
    assert desktop.main(["--host", host, "--port", "8123", "--no-tray"]) == 0
    desktop.uvicorn.Config.assert_called_once_with(desktop.app, host=host, port=8123, log_level="info")
    desktop.open_browser.assert_called_once_with(f"http://{browser_host}:8123")
    desktop.uvicorn.Server.return_value.run.assert_called_once()
    desktop.SingleInstanceGuard.return_value.release.assert_called_once()


@pytest.mark.parametrize("size", [32, 64, 128])
def test_icon_image_is_rgba_branded_and_font_independent(desktop, size):
    image = desktop.build_icon_image(size)
    assert image.mode == "RGBA" and image.size == (size, size)
    colors = set(image.getdata())
    assert {(0, 0, 0, 0), (30, 58, 138, 255), (255, 255, 255, 255)} <= colors
    assert image.getpixel((0, 0)) == (0, 0, 0, 0)


def test_windows_single_click_explicitly_opens_browser_and_exit_menu(desktop):
    on_exit = Mock()
    launcher = desktop.TrayLauncher(url="http://127.0.0.1:8123", on_exit=on_exit)
    icon = launcher._build_icon()
    assert type(icon).__call__ is not FakeIcon.__call__
    icon()  # Windows WM_LBUTTONUP invokes precisely this, not Menu.__call__.
    desktop.webbrowser.open.assert_called_once_with(launcher.url)
    icon.base_activation.assert_not_called()
    assert [item.text for item in icon.menu if item is not FakeMenu.SEPARATOR] == [
        desktop.MENU_OPEN_LABEL, desktop.MENU_HEALTH_LABEL, desktop.MENU_EXIT_LABEL]
    assert icon.menu[0].default
    icon.menu[0].action(icon, icon.menu[0])
    assert desktop.webbrowser.open.call_count == 2
    icon.menu[-1].action(icon, icon.menu[-1])
    icon.menu[-1].action(icon, icon.menu[-1])
    on_exit.assert_called_once_with()


def test_concurrent_exit_and_stop_are_one_shot_and_graceful(desktop):
    server = SimpleNamespace(should_exit=False, force_exit=False)
    on_exit = Mock(side_effect=lambda: desktop.request_server_stop(server))
    launcher = desktop.TrayLauncher(on_exit=on_exit)
    thread = launcher.start(timeout=1)
    assert thread.is_alive() and launcher._icon.visible
    barrier = threading.Barrier(13)
    errors = []

    def race(index):
        try:
            barrier.wait(timeout=2)
            (launcher.request_exit if index % 2 else launcher.stop)()
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=race, args=(i,), daemon=True) for i in range(12)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(2)
        assert not errors
        assert not any(worker.is_alive() for worker in workers)
        on_exit.assert_called_once_with()
        launcher._icon.stop.assert_called_once_with()
        assert server.should_exit and not server.force_exit
    finally:
        launcher.stop(timeout=1)
    assert not thread.is_alive()


def test_exit_before_backend_ready_is_deferred_not_lost(desktop, monkeypatch):
    entered, ready = threading.Event(), threading.Event()

    class DelayedIcon(FakeIcon):
        def run(self, setup):
            entered.set()
            assert ready.wait(2)
            super().run(setup)

    monkeypatch.setattr(desktop.pystray, "Icon", DelayedIcon)
    callback = Mock()
    launcher = desktop.TrayLauncher(on_exit=callback)
    outcome = []

    def start():
        try:
            outcome.append(launcher.start(timeout=2))
        except BaseException as error:
            outcome.append(error)

    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    try:
        assert entered.wait(1)
        launcher.request_exit()
        launcher.stop(timeout=0)
        launcher._icon.stop.assert_not_called()
        ready.set()
        starter.join(2)
        launcher.stop(timeout=1)
        assert outcome == [None]
        assert not starter.is_alive() and not launcher._thread.is_alive()
        callback.assert_called_once_with()
        launcher._icon.stop.assert_called_once_with()
        assert not launcher._icon.visible
    finally:
        ready.set()
        launcher.stop(timeout=1)


def test_stop_before_start_never_enters_backend(desktop, monkeypatch):
    launcher = desktop.TrayLauncher()
    build = Mock()
    monkeypatch.setattr(launcher, "_build_icon", build)
    launcher.stop()
    assert launcher.start(timeout=0) is None
    build.assert_not_called()


@pytest.mark.parametrize("failure", ["build", "run", "visible", "unexpected-return"])
def test_tray_failures_surface_and_request_shutdown(desktop, monkeypatch, failure):
    class BrokenIcon(FakeIcon):
        def run(self, setup):
            if failure == "run":
                raise RuntimeError("backend run failed")
            if failure == "visible":
                self.reject_visibility = True
                setup(self)
            # Returning without a requested stop must be reported, too.

        @property
        def visible(self):
            return getattr(self, "_visible", False)

        @visible.setter
        def visible(self, value):
            if getattr(self, "reject_visibility", False):
                raise RuntimeError("visibility failed")
            self._visible = value

    monkeypatch.setattr(desktop.pystray, "Icon", BrokenIcon)
    callback = Mock()
    launcher = desktop.TrayLauncher(on_exit=callback)
    if failure == "build":
        monkeypatch.setattr(launcher, "_build_icon", Mock(side_effect=RuntimeError("image failed")))
    try:
        with pytest.raises(RuntimeError):
            launcher.start(timeout=1)
        assert launcher.error is not None
        callback.assert_called_once_with()
    finally:
        launcher.stop(timeout=1)
    assert not launcher._thread.is_alive()


def test_windows_missing_tray_fails_but_no_tray_mode_runs(desktop, monkeypatch):
    monkeypatch.setattr(desktop, "PYSTRAY_AVAILABLE", False)
    callback = Mock()
    with pytest.raises(RuntimeError, match="no-tray"):
        desktop.TrayLauncher(on_exit=callback).start(timeout=0)
    callback.assert_called_once_with()
    monkeypatch.setattr(desktop, "TrayLauncher", Mock(side_effect=AssertionError("tray constructed")))
    assert desktop.main(["--no-tray", "--no-browser"]) == 0
    desktop.open_browser.assert_not_called()
    desktop.uvicorn.Server.return_value.run.assert_called_once()


@pytest.mark.parametrize("mode", ["normal", "exit-before-run", "server-error", "tray-start-error", "tray-runtime-error"])
def test_main_server_tray_lifecycle_and_failure_cleanup(desktop, monkeypatch, mode):
    server = desktop.uvicorn.Server.return_value
    launchers = []

    class ControlledTray:
        def __init__(self, url, on_exit):
            self.url, self.on_exit, self.error = url, on_exit, None
            self.stop = Mock()
            launchers.append(self)

        def start(self):
            if mode == "exit-before-run":
                self.on_exit()
            elif mode == "tray-start-error":
                raise RuntimeError("tray startup failed")

    def run():
        if mode == "server-error":
            raise RuntimeError("server failed")
        if mode == "tray-runtime-error":
            launchers[0].error = RuntimeError("tray died during serving")
            launchers[0].on_exit()

    server.run.side_effect = run
    monkeypatch.setattr(desktop, "TrayLauncher", ControlledTray)
    report = Mock()
    monkeypatch.setattr(desktop, "report_desktop_error", report)
    assert desktop.main([]) == (1 if mode.endswith("error") else 0)
    assert len(launchers) == 1
    launchers[0].stop.assert_called_once_with()
    desktop.SingleInstanceGuard.return_value.release.assert_called_once_with()
    assert server.should_exit and not server.force_exit
    if mode in ("exit-before-run", "tray-start-error"):
        server.run.assert_not_called()
        desktop.open_browser.assert_not_called()
    else:
        server.run.assert_called_once_with()
    assert report.call_count == (1 if mode.endswith("error") else 0)


def test_windows_error_is_visible_without_console(desktop, monkeypatch):
    message_box = Mock()
    monkeypatch.setitem(sys.modules, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(user32=SimpleNamespace(MessageBoxW=message_box))))
    desktop.report_desktop_error(RuntimeError("tray backend unavailable"))
    message_box.assert_called_once()
    args = message_box.call_args.args
    assert "tray backend unavailable" in args[1] and "--no-tray" in args[1]
    assert args[3] == 0x10


def test_startup_timeout_still_stops_backend_when_it_becomes_ready(desktop, monkeypatch):
    release = threading.Event()

    class SlowIcon(FakeIcon):
        def run(self, setup):
            assert release.wait(2)
            super().run(setup)

    monkeypatch.setattr(desktop.pystray, "Icon", SlowIcon)
    callback = Mock()
    launcher = desktop.TrayLauncher(on_exit=callback)
    try:
        with pytest.raises(RuntimeError, match="Timed out"):
            launcher.start(timeout=0.02)
        callback.assert_called_once_with()
    finally:
        release.set()
        launcher.stop(timeout=1)
    assert not launcher._thread.is_alive()
    launcher._icon.stop.assert_called_once_with()


def test_exit_callback_failure_does_not_prevent_backend_stop(desktop):
    callback = Mock(side_effect=RuntimeError("callback failed"))
    launcher = desktop.TrayLauncher(on_exit=callback)
    thread = launcher.start(timeout=1)
    try:
        launcher.request_exit()
        launcher.request_exit()
    finally:
        launcher.stop(timeout=1)
    callback.assert_called_once_with()
    launcher._icon.stop.assert_called_once_with()
    assert not thread.is_alive()


def test_browser_error_is_contained(desktop):
    desktop.webbrowser.open.side_effect = OSError("no browser")
    assert desktop.TrayLauncher().open_browser() is False


@pytest.mark.parametrize("accepted", [True, False])
def test_windows_shell_registration_result_is_checked(desktop, monkeypatch, accepted):
    launcher = desktop.TrayLauncher()
    icon = launcher._build_icon()
    icon._hwnd, icon._icon_handle = 123, 456
    icon._assert_icon_handle = Mock()
    notify = Mock(return_value=accepted)
    fields = Mock(side_effect=lambda **kwargs: kwargs)
    native = SimpleNamespace(Shell_NotifyIcon=notify, NOTIFYICONDATAW=fields,
                             NIM_ADD=0, NIF_MESSAGE=1, NIF_ICON=2, NIF_TIP=4, WM_NOTIFY=99)
    monkeypatch.setitem(sys.modules, "pystray._util", SimpleNamespace(win32=native))
    monkeypatch.setitem(sys.modules, "ctypes", SimpleNamespace(sizeof=lambda _: 100))
    if accepted:
        icon._show()
    else:
        with pytest.raises(RuntimeError, match="could not register"):
            icon._show()
    icon._assert_icon_handle.assert_called_once_with()
    notify.assert_called_once()
    assert fields.call_args.kwargs["uFlags"] == 7
    assert fields.call_args.kwargs["hIcon"] == 456


def test_explorer_restart_registration_failure_requests_shutdown(desktop, monkeypatch):
    def restart(icon, wparam, lparam):
        raise RuntimeError("Explorer registration failed")
    monkeypatch.setattr(FakeIcon, "_on_taskbarcreated", restart, raising=False)
    callback = Mock()
    launcher = desktop.TrayLauncher(on_exit=callback)
    icon = launcher._build_icon()
    icon._on_taskbarcreated(0, 0)
    assert str(launcher.error) == "Explorer registration failed"
    callback.assert_called_once_with()


def test_native_visibility_hang_does_not_block_startup_timeout(desktop, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    class HangingIcon(FakeIcon):
        @property
        def visible(self):
            return getattr(self, "_visible", False)
        @visible.setter
        def visible(self, value):
            if value:
                entered.set()
                assert release.wait(2)
            self._visible = value
    monkeypatch.setattr(desktop.pystray, "Icon", HangingIcon)
    callback = Mock()
    launcher = desktop.TrayLauncher(on_exit=callback)
    errors = []
    def start():
        try:
            launcher.start(timeout=0.05)
        except RuntimeError as error:
            errors.append(error)
        finally:
            finished.set()
    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    try:
        assert entered.wait(1)
        assert finished.wait(0.5), "native visibility call blocked timeout"
        assert errors and "Timed out" in str(errors[0])
        callback.assert_called_once_with()
    finally:
        release.set()
        starter.join(2)
        launcher.stop(timeout=1)
    launcher._icon.stop.assert_called_once_with()
    assert not launcher._thread.is_alive()


def test_runtime_requirements_pin_tested_tray_stack():
    lines = (ROOT / "requirements.txt").read_text().splitlines()
    assert "pystray==0.19.5" in lines
    assert "Pillow==12.0.0" in lines


@pytest.mark.parametrize("missing", [None, "pystray._win32", "native-imaging", "distribution"])
def test_windows_spec_hiddenimports_and_build_guards(missing):
    # Execute the real spec through its analysis guards, not a copied assertion
    # or PyInstaller build. No Windows imports/native DLLs are needed on Linux.
    tree = ast.parse((ROOT / "uniter.spec").read_text())
    prefix = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pyz" for t in node.targets):
            break
        prefix.append(node)
    required = {"pystray", "pystray._win32", "pystray._util.win32", "PIL.Image", "PIL.ImageDraw"}
    bundled = required - {missing}
    version = Mock(return_value="installed")
    if missing == "distribution":
        from importlib.metadata import PackageNotFoundError
        version.side_effect = PackageNotFoundError("pystray")
    real_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name == "sys":
            return SimpleNamespace(platform="win32")
        if name == "importlib.metadata":
            return SimpleNamespace(version=version)
        if name == "pystray":
            return SimpleNamespace(_win32=object())
        return real_import(name, *args, **kwargs)

    analysis = Mock(return_value=SimpleNamespace(
        pure=[(name, "unused", "PYMODULE") for name in bundled],
        binaries=[] if missing == "native-imaging" else [("PIL/_imaging.pyd", "unused", "EXTENSION")]))
    namespace = {"__builtins__": {**vars(builtins), "__import__": isolated_import}, "Analysis": analysis}
    code = compile(ast.Module(body=prefix, type_ignores=[]), str(ROOT / "uniter.spec"), "exec")
    if missing:
        expected = PackageNotFoundError if missing == "distribution" else RuntimeError
        with pytest.raises(expected):
            exec(code, namespace)
    else:
        exec(code, namespace)
        assert required | {"PIL._imaging"} <= set(analysis.call_args.kwargs["hiddenimports"])
        assert {call.args[0] for call in version.call_args_list} == {"pystray", "Pillow"}
