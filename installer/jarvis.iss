; Inno Setup 6 script for J.A.R.V.I.S.
; Build the app first (python build_exe.py), then either:
;   python build_exe.py --installer-only
;   "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" installer\jarvis.iss
; Output: dist\installer\JARVIS-Setup-<version>.exe
;
; Per-user install (no UAC prompt) into %LOCALAPPDATA%\Programs\JARVIS.
; User data (%LOCALAPPDATA%\JARVIS, %APPDATA%\Jarvis: config, logs, voice models) is never touched.

#define AppName      "J.A.R.V.I.S."
#define AppExeName   "JARVIS.exe"
#define AppPublisher "Muhammad Talib Ibrahim"
#define AppURL       "https://github.com/TalibIbrahim/JARVIS"
; Must match SetCurrentProcessExplicitAppUserModelID in main.py so toasts and taskbar grouping attach to the shortcut.
#define AppUserModelID "JARVIS.TouchlessPC.Control.1.1"
#define SourceDir    AddBackslash(SourcePath) + "..\dist\JARVIS"

#if !FileExists(SourceDir + "\" + AppExeName)
  #error "dist\JARVIS\JARVIS.exe not found - run python build_exe.py first"
#endif

; build_exe.py passes /DAppVersion=<version.py APP_VERSION>; otherwise read it from the built exe's version resource.
#ifndef AppVersion
  #define AppVersion GetStringFileInfo(SourceDir + "\" + AppExeName, "ProductVersion")
#endif

[Setup]
AppId={{DD3CEDF4-48ED-4CB7-920C-BF791F10C598}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
VersionInfoCompany={#AppPublisher}
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\JARVIS
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
UsePreviousAppDir=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SetupIconFile=..\assets\jarvis.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
OutputDir=..\dist\installer
OutputBaseFilename=JARVIS-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Start {#AppName} when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[InstallDelete]
; Drop the previous version's runtime so stale DLLs/modules never mix with the new build (no user data lives here).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; AppUserModelID: "{#AppUserModelID}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon

[Registry]
; Same key/value name/command format as autostart.py, so the in-app "Start with Windows" toggle sees it.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JARVIS"; ValueData: """{app}\{#AppExeName}"" --background"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
; Silent updates from the in-app updater relaunch Jarvis in the background once files are replaced.
Filename: "{app}\{#AppExeName}"; Parameters: "--background"; WorkingDir: "{app}"; Flags: nowait; Check: WizardSilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The Run value may also have been created by the in-app toggle (autostart.py), not just the installer task.
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'JARVIS');
end;
