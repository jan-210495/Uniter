# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys
from importlib.metadata import version

# Hidden imports cannot install absent dependencies. Fail the build rather than
# shipping a windowed executable with no way to exit it from the tray.
for distribution in ("pystray", "Pillow"):
    version(distribution)  # Raises PackageNotFoundError when not installed.

if sys.platform == "win32":
    from pystray import _win32  # Verify the actual Windows backend imports.
    from PIL import Image, ImageDraw

block_cipher = None

ROOT = Path.cwd()

datas = [
    (str(ROOT / "backend" / "templates"), "backend/templates"),
    (str(ROOT / "backend" / "static"), "backend/static"),
]

hiddenimports = [
    "aiosqlite",
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "starlette",
    "starlette.routing",
    "starlette.middleware",
    "starlette.responses",
    "starlette.templating",
    "starlette.staticfiles",
    "fastapi",
    "jinja2",
    "pydantic",
    "sqlalchemy",
    "sqlalchemy.ext.asyncio",
    "sqlalchemy.dialects.sqlite",
    "python_multipart",
    "pystray",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
]
if sys.platform == "win32":
    hiddenimports.extend(["pystray._win32", "pystray._util.win32", "PIL._imaging"])

a = Analysis(
    ["backend/desktop.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

if sys.platform == "win32":
    bundled_modules = {entry[0] for entry in a.pure}
    required = {"pystray", "pystray._win32", "pystray._util.win32", "PIL.Image", "PIL.ImageDraw"}
    missing = required - bundled_modules
    if missing:
        raise RuntimeError(f"Tray modules missing from Windows bundle: {sorted(missing)}")
    if not any("_imaging" in entry[0] for entry in a.binaries):
        raise RuntimeError("Pillow native imaging extension missing from Windows bundle")

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="uniter-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="uniter-server",
)
