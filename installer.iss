; Inno Setup скрипт для Оснастка-Маркет.
; Inno Setup — безкоштовний інструмент для Windows-інсталяторів:
; https://jrsoftware.org/isinfo.php
;
; Використання:
;   1. Спочатку виконай build_exe.bat — щоб з'явився dist\OsnastkaMarket.exe
;   2. Встанови Inno Setup, відкрий цей файл (installer.iss) в ньому
;   3. Build -> Compile (або клавіша F9)
;   4. Готовий інсталятор з'явиться в Output\OsnastkaMarket-Setup.exe
;
; Або збери повністю автоматично через GitHub Actions — див.
; .github/workflows/build-windows-exe.yml (не потребує Windows-ПК взагалі).

#define MyAppName "Оснастка-Маркет"
#define MyAppVersion "1.0"
#define MyAppExeName "OsnastkaMarket.exe"

[Setup]
AppId={{8F2B1E4A-6C3D-4A9E-9B1F-OSNASTKAMKT01}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\OsnastkaMarket
DefaultGroupName=Оснастка-Маркет
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=OsnastkaMarket-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
SetupIconFile=icon.ico
; Встановлення в Program Files не потребує адмінських прав на самі ДАНІ
; програми — app.py свідомо зберігає базу даних, логи й бекапи в
; %LOCALAPPDATA%\OsnastkaMarket (домашня тека користувача), а не поруч із .exe.
PrivilegesRequired=lowest

[Languages]
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Tasks]
Name: "desktopicon"; Description: "Створити ярлик на робочому столі"; GroupDescription: "Додаткові ярлики:"

[Files]
Source: "dist\OsnastkaMarket.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Оснастка-Маркет"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Видалити Оснастка-Маркет"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Оснастка-Маркет"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустити Оснастка-Маркет"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Питання видалення бази даних користувача при видаленні програми свідомо
; НЕ додано сюди — щоб деінсталяція програми ніколи не видаляла реальні
; робочі дані компанії без явної окремої дії користувача.
