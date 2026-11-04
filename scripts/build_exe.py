#!/usr/bin/env python3
"""Build script for compiling the Uniter desktop executable via PyInstaller."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_project_root() -> Path:
    """Return project root directory containing uniter.spec."""
    script_dir = Path(__file__).resolve().parent
    if (script_dir.parent / "uniter.spec").exists():
        return script_dir.parent
    cwd = Path.cwd()
    if (cwd / "uniter.spec").exists():
        return cwd
    return script_dir.parent


def build_executable(
    spec_path: Path | None = None,
    clean: bool = True,
    dist_path: Path | None = None,
    work_path: Path | None = None,
) -> int:
    """Invoke PyInstaller to build the desktop server executable."""
    root = find_project_root()
    spec = spec_path or (root / "uniter.spec")

    if not spec.exists():
        print(f"Error: Specification file not found at {spec}", file=sys.stderr)
        return 1

    pyinstaller_bin = shutil.which("pyinstaller")
    if pyinstaller_bin:
        cmd = [pyinstaller_bin]
    else:
        cmd = [sys.executable, "-m", "PyInstaller"]

    if clean:
        cmd.append("--clean")

    if dist_path:
        cmd.extend(["--distpath", str(dist_path)])

    if work_path:
        cmd.extend(["--workpath", str(work_path)])

    cmd.append(str(spec))

    print(f"Building desktop executable with command: {' '.join(cmd)}")
    print(f"Working directory: {root}")

    try:
        process = subprocess.run(cmd, cwd=str(root))
        if process.returncode == 0:
            print("Desktop executable build completed successfully.")
        else:
            print(f"Desktop executable build failed with exit code {process.returncode}.", file=sys.stderr)
        return process.returncode
    except FileNotFoundError:
        print("Error: PyInstaller is not installed or not available on PATH.", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Unexpected error during build: {error}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Parse CLI options and execute the build."""
    parser = argparse.ArgumentParser(description="Build Uniter standalone executable")
    parser.add_argument("--spec", type=Path, default=None, help="Path to .spec file (default: uniter.spec)")
    parser.add_argument("--no-clean", dest="clean", action="store_false", default=True, help="Do not pass --clean to PyInstaller")
    parser.add_argument("--distpath", type=Path, default=None, help="Where to put the bundled app")
    parser.add_argument("--workpath", type=Path, default=None, help="Where to put all temporary work files")
    args = parser.parse_args(argv)

    return build_executable(
        spec_path=args.spec,
        clean=args.clean,
        dist_path=args.distpath,
        work_path=args.workpath,
    )


if __name__ == "__main__":
    sys.exit(main())
