# Windows v1.0.5 packaging readiness (publication is separate)

## Version and unchanged installation contract

`installer/uniter.iss`'s single `#define AppVersion "1.0.5"` is the
authoritative **release-facing** version. Its AppVersion, AppVerName,
VersionInfoVersion and VersionInfoProductVersion directives all derive from it.
There is no second runtime constant or new version UI. Historical v1.0.4 docs,
fixtures and schema markers are not current-release constants and stay intact.
The published v1.0.4 annotated tag object must remain
`6ecdb92b72bd55a7f7c3950a4855f4739b9fc7eb` (its target commit is
`ac5fe062fefaadfd938559eab22c1399a4558feb`). No tag operations are needed here.

The AppId remains `{B1F4C2D9-5E3A-4F1B-8C7D-6F9A2B3C4D5E}`. Install paths,
startup checkbox defaults, account/data preservation, elevation, post-install
launch and uninstall behavior are unchanged. This slice does not assert that
fresh-install v1.0.5 supports migrating old customer/debt data. Installer identity
continuity and application database compatibility are different concerns.

## Portable checks (not a Windows compiler)

From the repository root, with Python 3.12:

```text
python -m pip install -r requirements-test.txt
python scripts/check_release.py
python scripts/check_release.py --tag v1.0.5
python -m pytest backend/test_release_packaging.py backend/test_desktop.py --tb=short -q
```

`--tag` checks a string only: it never creates, moves or pushes a tag. It rejects
a mismatch before build/upload. The script checks the stable installer identity,
version derivations and required source inputs without importing the app or
writing artifacts. The YAML tests use PyYAML BaseLoader, deliberately preserving
the literal GitHub `on` key rather than YAML 1.1's boolean conversion. PyYAML and
the XLSX test reader remain **test-only**; runtime `requirements.txt` is unchanged.
`uniter.spec` explicitly excludes test-only spreadsheet/parser/pytest modules.

## Windows build and artifact verification (no publication)

Use a fresh Windows checkout at the reviewed full commit, Python 3.12, and Inno
Setup **6** installed at its standard path. Do not reuse a dist directory with
old installers: the existing Inno fallback wildcard can otherwise include them.
Do not run the installer or seed a database as part of this build verification.
In a PowerShell session at the checkout root:

```powershell
$ErrorActionPreference = 'Stop'
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'runtime dependency installation failed' }
python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed' }
python scripts/check_release.py
if ($LASTEXITCODE -ne 0) { throw 'release input validation failed' }
python scripts/build_exe.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
./scripts/verify_windows_package.ps1 -Stage Bundle
foreach ($Path in @('dist/UniterSetup.exe', 'installer/Output/UniterSetup.exe')) {
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force -ErrorAction Stop }
}
& 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe' installer\uniter.iss
if ($LASTEXITCODE -ne 0) { throw 'Inno compilation failed' }
if (-not (Test-Path 'dist/UniterSetup.exe' -PathType Leaf)) { throw 'installer missing' }
if ((Get-Item 'dist/UniterSetup.exe').Length -le 0) { throw 'installer empty' }
New-Item -ItemType Directory -Force -Path 'installer/Output' | Out-Null
Copy-Item 'dist/UniterSetup.exe' 'installer/Output/UniterSetup.exe' -Force
./scripts/verify_windows_package.ps1 -Stage Installer
```

The PyInstaller onedir bundle remains `dist/uniter-server/`; the installer is
`dist/UniterSetup.exe`, with the identical verified upload copy at
`installer/Output/UniterSetup.exe`. The Windows-only checker fails for missing or
empty bundle/installer/copy files, checks the installer's actual PE numeric file
and product resource versions are **1.0.5.0**, checks product name `Uniter`, and
compares the two installer copies' SHA256. It does not launch either executable,
create accounts, publish assets or verify a code signature. No signing setup is
introduced. Build tools remain installed by the existing pip/Chocolatey paths;
this is not a bit-for-bit reproducible/signed build claim.

## Existing CI verification vs release publishing

**Non-publishing path:** `.github/workflows/desktop-verify.yml` remains a separate
`contents: read` Windows job with its existing branch trigger and manual dispatch.
It now parses the PowerShell checker, builds the same spec via build_exe.py,
compiles the same Inno script using exactly the same compilation block as release,
and checks both output/version/hash gates. It uploads only **workflow artifacts**,
not GitHub Release assets. Select the reviewed feature/Arena branch in the
existing workflow's Run workflow selector (or dispatch only this workflow with
`gh workflow run desktop-verify.yml --ref REVIEWED_BRANCH`). Record the resulting
run URL, full source SHA and successful checks. Availability/permission/quota
errors are a real-build limitation, not evidence of a successful build.

**Publishing path:** `.github/workflows/release.yml` is still triggered **only**
by pushed `v*` tags, with its existing write permission and asset upload action.
Its input version/tag check, native-command failure checks, nonempty output
checks and installer version/hash check all precede uploads. Missing upload files
now fail explicitly in both upload actions. No branch/manual trigger, release
creation command or additional publishing mechanism is added.

**Do not push any tag or invoke the release workflow for this readiness slice.**
Publication requires a separate final owner approval after the integrated product
test and successful Windows build. The existing v1.0.4 tag must never be moved.
Do not manufacture a v1.0.5 tag merely to exercise CI. This procedure does not
claim an installation/upgrade/browser acceptance test; those are separate gates.

Linux remains the Python source-service path documented in
[VPS deployment](vps-deployment.md); its approved files are not modified here.

## Source-readiness verification evidence

Sandbox: Debian GNU/Linux 12 (bookworm), x86_64, Python **3.12.8**. Runtime
requirements are unchanged; only test requirements add pinned `PyYAML==6.0.2`
for packaging YAML checks. A clean verification environment installed
`requirements-test.txt`; `uv pip check --python .venv/bin/python` reported
`All installed packages are compatible`.

```text
python3 -m pytest backend/test_release_packaging.py backend/test_desktop.py --tb=short -q
49 passed in 2.76s
```

```text
python3 -m pytest backend/ --tb=short -q
903 passed, 724 warnings in 337.49s (0:05:37)
```

Warnings are existing dependency deprecations, not test failures.

`python3 scripts/check_release.py --tag v1.0.5` printed `1.0.5`. Python syntax
compilation passed for the input checker, unchanged build wrapper and packaging
tests. PyYAML 6.0.2 parsed all three workflow files with literal `on` preserved.
Complete source diff/whitespace checks were reviewed, and `deploy/vps/`,
`docs/vps-deployment.md` and `docs/vps-verification.md` have zero diff from base
`d21e78f4923fdf496ac2d5866b5d9332ca72331a`.

PowerShell, PyInstaller and Inno Setup were **not run on Linux**. The separate
Windows workflow is the place to validate native PowerShell parsing, the real
bundle, actual installer PE version metadata and artifact upload. Its run result
is separate CI evidence, not something these portable tests can fabricate. A
successful real Windows run remains required before final publication approval.
No installation/upgrade/browser/signature/VPS test is claimed by this slice.
