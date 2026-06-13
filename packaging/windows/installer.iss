; Inno Setup script for Friday.
; Compile after PyInstaller has produced dist\Friday:
;   iscc packaging\windows\installer.iss
; Output: packaging\windows\Output\FridaySetup.exe

#define AppName "Friday"
#define AppVersion "1.0.0"
#define AppPublisher "Keller Leonard"
#define AppExeName "Friday.exe"

[Setup]
AppId={{8F2A41D6-2B7C-4E1B-9C93-5D04A7E61B23}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=FridaySetup
SetupIconFile=friday.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "autostart"; Description: "Start {#AppName} automatically when Windows starts"; \
    GroupDescription: "Startup:"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\Friday\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName} now"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop the tray (and with it the supervised core) before removing files.
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#AppExeName} /F /T"; \
    Flags: runhidden; RunOnceId: "KillFriday"
