; Inno Setup script for Session Deck (per-user install, no admin rights needed).
; Built by .github/workflows/release.yml:  ISCC /DAppVersion=1.0.0 installer\SessionDeck.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B2E8C1F-4A7D-4E3B-9C55-3D1F0A8E7B42}
AppName=Session Deck
AppVersion={#AppVersion}
AppVerName=Session Deck {#AppVersion}
AppPublisher=optimus-ook-prime-gluk
AppPublisherURL=https://github.com/optimus-ook-prime-gluk/session-deck
AppSupportURL=https://github.com/optimus-ook-prime-gluk/session-deck/issues
AppUpdatesURL=https://github.com/optimus-ook-prime-gluk/session-deck/releases
DefaultDirName={localappdata}\Programs\Session Deck
DefaultGroupName=Session Deck
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=SessionDeck-Setup-{#AppVersion}
SetupIconFile=..\assets\logo.ico
UninstallDisplayIcon={app}\SessionDeck.exe
UninstallDisplayName=Session Deck
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Open Session Deck when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\SessionDeck\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Session Deck"; Filename: "{app}\SessionDeck.exe"
Name: "{group}\Session Deck (demo with sample sessions)"; Filename: "{app}\SessionDeck.exe"; Parameters: "--demo"
Name: "{group}\Uninstall Session Deck"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Session Deck"; Filename: "{app}\SessionDeck.exe"; Tasks: desktopicon
Name: "{userstartup}\Session Deck"; Filename: "{app}\SessionDeck.exe"; Tasks: startup

[Run]
Filename: "{app}\SessionDeck.exe"; Description: "Open Session Deck now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM SessionDeck.exe"; Flags: runhidden; RunOnceId: "StopSessionDeck"

[UninstallDelete]
; the app's own cache, search index and window profile (your Claude Code data is never touched)
Type: filesandordirs; Name: "{localappdata}\SessionDeck"
