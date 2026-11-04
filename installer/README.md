# Building `UniterSetup.exe` (Windows installer)

Two-step process, both run **on Windows**:

1. **PyInstaller** produces the standalone app bundle.
2. **Inno Setup 6** compiles `installer/uniter.iss` into `UniterSetup.exe`.

## Prerequisites

- Windows 10/11 (64-bit) with Python 3.12+
- Project dependencies plus PyInstaller:

  ```powershell
  pip install -r requirements.txt
  pip install pyinstaller
  ```

- [Inno Setup 6](https://jrsoftware.org/isdl.php) (6.3 or newer recommended —
  the script uses `x64compatible` architecture mode). After installation,
  `ISCC.exe` lives in `C:\Program Files (x86)\Inno Setup 6\` by default.

## Step 1 — Build the app bundle

From the project root:

```powershell
python scripts/build_exe.py
```

This runs PyInstaller with a clean build using `uniter.spec` and produces:

```text
dist\\uniter-server\\uniter-server.exe   (+ supporting DLLs/data)
```

## Step 2 — Compile the installer

With `dist\\uniter-server\\` in place, compile the Inno Setup script:

```powershell
& "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe" installer\\uniter.iss
```

(Alternatively open `installer\\uniter.iss` in the Inno Setup Compiler GUI
and press **Build > Compile**.)

Output:

```text
dist\\UniterSetup.exe
```

## Installer behavior (what the wizard does)

- Installs to `C:\Program Files\Uniter` (64-bit aware).
- Creates a Start Menu folder with app + uninstall shortcuts.
- Optional Desktop shortcut (checkbox during install).
- Optional "Start with Windows" entry under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (value
  `Uniter`, **off by default**).
- Offers to launch the app after finishing (as the signed-in user).
- **Upgrades** reuse the previous install directory and replace binaries
  in-place via the fixed AppId GUID — never change it once released.
- **Uninstall** removes program files, shortcuts, and the Run entry only.

## Troubleshooting

- `Source: "..\\dist\\uniter-server\\*" not found` — run Step 1 first; the
  `.iss` resolves paths relative to the `installer\\` folder.
- Installer requests admin rights by design (Program Files target).
- To silently install: `UniterSetup.exe /VERYSILENT /SUPPRESSMSGBOXES`
  (the post-install launch checkbox is automatically skipped).
