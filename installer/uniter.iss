; ============================================================================
; UniterSetup.exe — Inno Setup 6 installer for Uniter (Mobile Shop Charging)
; ============================================================================
; Prerequisites: build the PyInstaller bundle first so that
;   dist\uniter-server\uniter-server.exe
; exists (run `python scripts/build_exe.py` from the project root), then
; compile this script with Inno Setup 6 (ISCC.exe).
; ============================================================================

#define AppName       "Uniter"
#define AppPublisher  "Uniter"
; Keep in sync with the tag/version used for releases.
#define AppVersion    "1.0.3"
#define AppExeName    "uniter-server.exe"
#define StartupRunValueName "Uniter"
#define AppDataSubDir "uniter\\data"

[Setup]
; Fixed AppId GUID: same GUID across releases -> upgrades replace binaries
; in-place (Inno auto-uninstalls the previous version) without prompting
; for a new directory. Do NOT change once released.
AppId={{B1F4C2D9-5E3A-4F1B-8C7D-6F9A2B3C4D5E}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
; Install destination: 64-bit Program Files, e.g. C:\Program Files\Uniter
DefaultDirName={autopf}\\Uniter
DefaultGroupName=Uniter
DisableProgramGroupPage=yes
; Reuse the previous install folder and Start Menu group on upgrades.
UsePreviousAppDir=yes
UsePreviousGroup=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
MinVersion=10.0
OutputDir=..\\dist
OutputBaseFilename=UniterSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\\{#AppExeName}

[Files]
; Onedir build (preferred)
Source: "..\\dist\\uniter-server\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
; Onefile fallback
Source: "..\\dist\\uniter-server.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\\dist\\*.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
; Start Menu folder shortcuts (always created).
Name: "{group}\\{#AppName}"; Filename: "{app}\\{#AppExeName}"; Comment: "Uniter — Mobile Shop Charging & Units Management"
Name: "{group}\\Uninstall {#AppName}"; Filename: "{uninstallexe}"
; Optional Desktop shortcut (user-selectable during install).
Name: "{autodesktop}\\{#AppName}"; Filename: "{app}\\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
; Windows startup entry — offered as a checkbox, OFF by default.
Name: "startupentry"; Description: "Start {#AppName} when you sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Registry]
; Launch on sign-in (current user only; toggled by the "startupentry" task).
Root: HKCU; Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Run"; ValueType: string; ValueName: "{#StartupRunValueName}"; ValueData: """{app}\\{#AppExeName}"""; Flags: uninsdeletevalue; Tasks: startupentry

[Run]
; Offer to start the app right after installation (as the signed-in user,
; not elevated).
Filename: "{app}\\{#AppExeName}"; Description: "Launch {#AppName} now"; Flags: postinstall skipifsilent nowait runasoriginaluser

; ---------------------------------------------------------------------------
; Uninstall behavior
; ---------------------------------------------------------------------------
; The standard Inno Setup uninstaller removes {app} (program files) and all
; icons/shortcuts/registry values listed above. User data is preserved.
