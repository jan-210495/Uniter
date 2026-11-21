"""Portable input/gating checks, NOT a Windows compiler or installer run."""
import ast
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_input_check', ROOT / 'scripts/check_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def workflow(name):
    # BaseLoader preserves YAML 1.2/GitHub's literal "on" key (YAML 1.1
    # SafeLoader otherwise interprets it as boolean True). All scalars strings.
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


def test_installer_version_derivation_and_stable_identity():
    assert release.check_release(ROOT, 'v1.0.5') == '1.0.5'
    script = (ROOT / 'installer/uniter.iss').read_text()
    assert release.DERIVED['AppId'] == '{{B1F4C2D9-5E3A-4F1B-8C7D-6F9A2B3C4D5E}'
    for directive in ('DefaultDirName={autopf}\\\\Uniter', 'UsePreviousAppDir=yes',
                      'UsePreviousGroup=yes', 'PrivilegesRequired=admin',
                      'OutputBaseFilename=UniterSetup', 'OutputDir=..\\\\dist',
                      'Flags: unchecked', 'Flags: uninsdeletevalue; Tasks: startupentry',
                      'Flags: postinstall skipifsilent nowait runasoriginaluser'):
        assert directive in script
    assert '[UninstallDelete]' not in script  # user data preservation unchanged
    assert 'VersionInfoVersion={#AppVersion}' in script
    assert 'VersionInfoProductVersion={#AppVersion}' in script
    with pytest.raises(ValueError, match='does not match'):
        release.check_release(ROOT, 'v1.0.4')


@pytest.mark.parametrize('replacement', [
    ('#define AppVersion    "1.0.5"', '#define AppVersion    "bad"'),
    ('VersionInfoVersion={#AppVersion}', 'VersionInfoVersion=1.0.4'),
    ('VersionInfoProductVersion={#AppVersion}', 'VersionInfoProductVersion=1.0.4'),
    ('AppVerName={#AppName} {#AppVersion}', 'AppVerName=Uniter 1.0.4'),
    ('AppId={{B1F4C2D9-5E3A-4F1B-8C7D-6F9A2B3C4D5E}', 'AppId=changed'),
])
def test_metadata_drift_fails_before_build(tmp_path, replacement):
    (tmp_path / 'installer').mkdir()
    content = (ROOT / 'installer/uniter.iss').read_text()
    old, new = replacement
    assert old in content
    (tmp_path / 'installer/uniter.iss').write_text(content.replace(old, new))
    with pytest.raises(ValueError):
        release.check_release(tmp_path)


def test_check_cli_is_read_only_and_returns_failure_for_mismatched_tag():
    args = [sys.executable, str(ROOT / 'scripts/check_release.py')]
    result = subprocess.run(args + ['--tag', 'v1.0.5'], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == '1.0.5'
    result = subprocess.run(args + ['--tag', 'v1.0.4'], capture_output=True, text=True)
    assert result.returncode == 1 and 'does not match' in result.stderr


def test_release_trigger_permissions_and_preupload_fail_closed_checks():
    data = workflow('release.yml')
    assert data['on'] == {'push': {'tags': ['v*']}}
    assert data['permissions'] == {'contents': 'write'}
    job = data['jobs']['build-windows-installer']
    assert job['runs-on'] == 'windows-latest' and job['permissions'] == {'contents': 'write'}
    steps = job['steps']
    runs = '\n'.join(step.get('run', '') for step in steps)
    assert 'python scripts/check_release.py --tag "$env:GITHUB_REF_NAME"' in runs
    gate = next(i for i, step in enumerate(steps) if step.get('run') == './scripts/verify_windows_package.ps1 -Stage Installer')
    for i, step in enumerate(steps):
        assert 'continue-on-error' not in step
        if step.get('uses', '').startswith(('softprops/action-gh-release@', 'actions/upload-artifact@')):
            assert i > gate
            assert step['with'].get('files', step['with'].get('path')) == 'installer/Output/UniterSetup.exe'
            if step['uses'].startswith('softprops'):
                assert step['if'] == "startsWith(github.ref, 'refs/tags/')"
                assert step['with']['fail_on_unmatched_files'] == 'true'
            else:
                assert step['with']['if-no-files-found'] == 'error'
    assert '$LASTEXITCODE -ne 0' in runs
    assert "throw 'Installer output missing'" in runs and "throw 'Installer output empty'" in runs
    assert 'pip install -r requirements-test.txt' not in runs


def test_nonpublishing_windows_workflow_uses_exact_compiler_inputs_and_read_only_token():
    data = workflow('desktop-verify.yml')
    assert data['permissions'] == {'contents': 'read'}
    assert set(data['on']) == {'push', 'workflow_dispatch'}
    assert data['on']['push'] == {'branches': ['fix/windows-tray-lan-v1.0.3']}
    steps = data['jobs']['windows-desktop']['steps']
    text = str(data)
    assert 'softprops/' not in text and 'gh release' not in text and 'git tag' not in text
    assert 'Parser]::ParseFile' in text
    release_steps = workflow('release.yml')['jobs']['build-windows-installer']['steps']
    def compiler_block(items):
        return next(step['run'] for step in items if 'ISCC.exe' in step.get('run', ''))
    assert compiler_block(steps) == compiler_block(release_steps)
    gate = next(i for i, step in enumerate(steps) if step.get('run') == './scripts/verify_windows_package.ps1 -Stage Installer')
    assert any(i > gate and step.get('with', {}).get('path') == 'installer/Output/UniterSetup.exe'
               for i, step in enumerate(steps))
    assert 'python scripts/build_exe.py' in text and 'verify_windows_package.ps1 -Stage Bundle' in text


def test_powershell_artifact_check_contract_and_parser_when_available():
    script = ROOT / 'scripts/verify_windows_package.ps1'
    text = script.read_text()
    for required in ('Win32NT', 'throw', '-PathType Leaf', '.Length -le 0',
                     'FileVersionInfo', 'FileMajorPart', 'ProductMajorPart',
                     '$Expected = "$Version.0"', 'Get-FileHash', '-Algorithm SHA256',
                     'dist/uniter-server/uniter-server.exe', 'dist/UniterSetup.exe',
                     'installer/Output/UniterSetup.exe'):
        assert required in text
    assert 'Start-Process' not in text and 'gh release' not in text
    if shutil.which('pwsh'):
        command = "$e=$null; $t=$null; [System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$t,[ref]$e)|Out-Null; if($e.Count){throw ($e|Out-String)}"
        subprocess.run(['pwsh', '-NoProfile', '-Command', command, str(script)], check=True)


def test_runtime_test_dependency_and_bundle_separation():
    runtime = (ROOT / 'requirements.txt').read_text().lower()
    tests = (ROOT / 'requirements-test.txt').read_text()
    assert '-r requirements.txt' in tests and 'openpyxl==3.1.5' in tests and 'PyYAML==6.0.2' in tests
    assert all(name not in runtime for name in ('openpyxl', 'et_xmlfile', 'pyyaml'))
    tree = ast.parse((ROOT / 'uniter.spec').read_text())
    analysis = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Analysis')
    excludes = ast.literal_eval(next(kw.value for kw in analysis.keywords if kw.arg == 'excludes'))
    assert {'openpyxl', 'et_xmlfile', 'pytest'} <= set(excludes)
    assert "['backend/desktop.py']" == ast.unparse(analysis.args[0])
    assert 'requirements-test' not in (ROOT / '.github/workflows/desktop-verify.yml').read_text()
    # Parse every workflow using the real parser; do not execute any workflow.
    for path in (ROOT / '.github/workflows').glob('*.yml'):
        parsed = workflow(path.name)
        assert 'on' in parsed and True not in parsed


def test_inno_product_name_padding_contract(tmp_path):
    """Portable source contract; synthetic cases are NOT native PE evidence."""
    text = (ROOT / 'scripts/verify_windows_package.ps1').read_text()
    assert '$RawProductName = [string]$Info.ProductName' in text
    assert "$ProductName = $RawProductName.TrimEnd([char]' ')" in text
    assert "if ($ProductName -cne 'Uniter') { throw 'Unexpected installer product name.' }" in text
    assert 'raw=<{0}> (length {1}); normalized=<{2}>' in text
    assert '.Trim()' not in text.split('$RawProductName =', 1)[1]
    assert 'VersionInfoProductName={#AppName}' in (ROOT / 'installer/uniter.iss').read_text()
    if shutil.which('pwsh'):
        # Exercise the EXACT normalization/comparison statements from the checker
        # against synthetic values, without pretending to inspect a real EXE.
        normalization = next(line.strip() for line in text.splitlines()
                             if line.strip().startswith('$ProductName ='))
        validation = next(line.strip() for line in text.splitlines()
                          if line.strip().startswith("if ($ProductName -cne"))
        script = tmp_path / 'product-name-cases.ps1'
        script.write_text("""$ErrorActionPreference = 'Stop'
$Cases = @(
    @{Value = 'Uniter'; Valid = $true},
    @{Value = ('Uniter' + (' ' * 54)); Valid = $true},
    @{Value = ''; Valid = $false},
    @{Value = ' Uniter'; Valid = $false},
    @{Value = 'uniter'; Valid = $false},
    @{Value = 'OtherProduct'; Valid = $false},
    @{Value = "Uniter`t"; Valid = $false}
)
foreach ($Case in $Cases) {
    $RawProductName = [string]$Case.Value
    $Accepted = $true
    try {
""" + normalization + '\n' + validation + """
    } catch { $Accepted = $false }
    if ($Accepted -ne $Case.Valid) { throw 'ProductName padding regression' }
}
""")
        subprocess.run(['pwsh', '-NoProfile', '-File', str(script)], check=True)
