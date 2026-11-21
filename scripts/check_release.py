#!/usr/bin/env python3
"""Non-publishing release-input check. Inno AppVersion is authoritative.

No application imports, build tools, network, Git writes or artifact generation.
An optional tag must match exactly; historical migration/docs versions are not
release constants and must never be mass-replaced.
"""
import argparse
from pathlib import Path
import re
import sys

APP_ID = '{{B1F4C2D9-5E3A-4F1B-8C7D-6F9A2B3C4D5E}'
DERIVED = {
    'AppVersion': '{#AppVersion}',
    'AppVerName': '{#AppName} {#AppVersion}',
    'VersionInfoVersion': '{#AppVersion}',
    'VersionInfoProductVersion': '{#AppVersion}',
    'AppId': APP_ID,
}


def check_release(root: Path, tag: str | None = None) -> str:
    script = (root / 'installer/uniter.iss').read_text(encoding='utf-8')
    versions = re.findall(r'^#define\s+AppVersion\s+"(\d+\.\d+\.\d+)"\s*$', script, re.M)
    if len(versions) != 1:
        raise ValueError('Expected exactly one three-component Inno AppVersion')
    version = versions[0]
    if any(int(part) > 65535 for part in version.split('.')):
        raise ValueError('Version components must fit Windows version resources')
    for key, expected in DERIVED.items():
        actual = re.findall(r'^' + re.escape(key) + r'=(.*)$', script, re.M)
        if actual != [expected]:
            raise ValueError(f'Installer {key} must be {expected!r}')
    if tag is not None and tag != 'v' + version:
        raise ValueError(f'Tag {tag!r} does not match installer version v{version}')
    for relative in ('uniter.spec', 'scripts/build_exe.py', 'requirements.txt',
                     'backend/desktop.py', 'backend/templates/base.html', 'backend/static/manifest.webmanifest'):
        if not (root / relative).is_file():
            raise ValueError(f'Missing packaging input: {relative}')
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--tag', help='Exact prospective release tag; never creates one')
    args = parser.parse_args()
    try:
        print(check_release(args.root, args.tag))
    except (OSError, ValueError) as error:
        print(f'Release input check failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
