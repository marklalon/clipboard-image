; Little Helper - Inno Setup Script
; Requires Inno Setup 6.0 or later

#define MyAppName "Little Helper"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Little Helper"
#define MyAppExeName "LittleHelper.exe"

[Setup]
AppId={{8A3F4B5C-6D7E-8F9A-0B1C-2D3E4F5A6B7C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=LittleHelper-Setup
SetupIconFile=res\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Launch at startup (no UAC prompt)"; GroupDescription: "Options:"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "res\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; IconFilename: "{app}\icon.ico"

[Registry]
; (autostart is handled via Task Scheduler, not registry Run key, to avoid UAC prompt)

[Code]
procedure CreateStartupTask();
var
  ResultCode: Integer;
begin
  Exec('schtasks.exe',
    '/create /tn "LittleHelper" /tr "\"' + ExpandConstant('{app}\{#MyAppExeName}') + '\""' +
    ' /sc ONLOGON /rl HIGHEST /f',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure DeleteStartupTask();
var
  ResultCode: Integer;
begin
  Exec('schtasks.exe', '/delete /tn "LittleHelper" /f',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('autostart') then
      CreateStartupTask();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    DeleteStartupTask();
end;

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent shellexec

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
