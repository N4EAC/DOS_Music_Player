@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title DOS Music Player - Standalone Builder

echo =====================================================
echo   DOS Music Player v1.2.6 - Responsive Stream Control
echo =====================================================
echo.

python -m pip install --upgrade pip pyinstaller
if errorlevel 1 goto :fail
python -m pip uninstall -y pygame >nul 2>&1
python -m pip install --upgrade --only-binary=:all: pygame-ce
if errorlevel 1 goto :fail

if not exist "vendor\ffplay.exe" goto :download_ffplay
if not exist "vendor\SDL2.dll" goto :download_ffplay
goto :build

:download_ffplay
echo.
echo Downloading portable FFplay and its required DLL files...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$zip=Join-Path $PWD 'ffmpeg-release-essentials.zip';" ^
  "$tmp=Join-Path $PWD '_ffmpeg_extract';" ^
  "if(Test-Path $zip){Remove-Item $zip -Force};" ^
  "if(Test-Path $tmp){Remove-Item $tmp -Recurse -Force};" ^
  "Invoke-WebRequest -UseBasicParsing 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zip;" ^
  "Expand-Archive $zip $tmp -Force;" ^
  "$ff=Get-ChildItem $tmp -Recurse -Filter ffplay.exe | Select-Object -First 1;" ^
  "if(-not $ff){throw 'ffplay.exe was not found in the downloaded archive'};" ^
  "$bin=$ff.Directory.FullName;" ^
  "$vendor=Join-Path $PWD 'vendor'; New-Item -ItemType Directory -Force $vendor | Out-Null;" ^
  "Get-ChildItem $vendor -File -ErrorAction SilentlyContinue | Remove-Item -Force;" ^
  "Copy-Item $ff.FullName (Join-Path $vendor 'ffplay.exe') -Force;" ^
  "Get-ChildItem $bin -Filter '*.dll' | Copy-Item -Destination $vendor -Force;" ^
  "if(-not (Test-Path (Join-Path $vendor 'SDL2.dll'))){Write-Warning 'SDL2.dll was not found by name; all available DLLs were still copied.'};" ^
  "Remove-Item $tmp -Recurse -Force; Remove-Item $zip -Force"
if errorlevel 1 goto :fail

:build
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q DOSMusicPlayer.spec 2>nul

set "ADD_DLLS="
for %%F in (vendor\*.dll) do call set "ADD_DLLS=%%ADD_DLLS%% --add-binary ""%%F;ffplay"""

python -m PyInstaller --noconfirm --clean --onefile ^
  --name DOSMusicPlayer ^
  --icon dos_music_player.ico ^
  --add-binary "vendor\ffplay.exe;ffplay" ^
  %ADD_DLLS% ^
  --collect-all pygame ^
  dos_music_player.py
if errorlevel 1 goto :fail

echo.
echo BUILD COMPLETE
echo Executable: dist\DOSMusicPlayer.exe
echo VLC is NOT required. FFplay and its DLL runtime are embedded.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Review the error messages above.
pause
exit /b 1
