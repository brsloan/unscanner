; Inno Setup script for the Unscanner installer. scripts/build_installer.py runs it with
;   /DAppVersion=... /DSourceDir=<PyInstaller's Unscanner folder> /DOutputDir=... /DIconFile=...
; It installs for the current user only, into AppData\Local\Programs\Unscanner: no administrator
; rights and no admin prompt. Documents stay in Documents\Unscanner (see src/unscanner/app.py), so
; upgrading or uninstalling never touches them.

[Setup]
; Never change AppId: it is how an upgrade finds the installed copy.
AppId={{717F4491-2194-4F78-9C6C-FBAB87F4C7B4}
AppName=Unscanner
AppVersion={#AppVersion}
AppVerName=Unscanner {#AppVersion}
DefaultDirName={autopf}\Unscanner
DefaultGroupName=Unscanner
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Unscanner-Setup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\Unscanner.exe
UninstallDisplayName=Unscanner
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Close a running Unscanner before replacing its files.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; An upgrade replaces the whole program; files a newer version no longer has must not linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Unscanner"; Filename: "{app}\Unscanner.exe"; Comment: "Turn scanned PDFs into accessible HTML and EPUB"
Name: "{autodesktop}\Unscanner"; Filename: "{app}\Unscanner.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Unscanner.exe"; Description: "Start Unscanner"; Flags: nowait postinstall skipifsilent
