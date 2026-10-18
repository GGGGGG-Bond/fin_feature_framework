@echo off
setlocal
chcp 65001 >nul
where wsl.exe >nul 2>nul
if errorlevel 1 (
  echo WSL is required for recomputing. Open the Release results ZIP for a no-install preview.
  if not "%FIN_DEMO_NO_OPEN%"=="1" pause
  exit /b 2
)
echo Running in WSL. The Linux distribution needs Python 3.12+ with venv and pip.
set "FIN_DEMO_SOURCE=%~dp0."
rem Prefer FIN_DEMO_DISTRO if provided; otherwise probe installed distributions.
rem No default-distribution or PowerShell execution-policy settings are changed.
powershell.exe -NoProfile -Command "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $ErrorActionPreference='SilentlyContinue'; $ds=if($env:FIN_DEMO_DISTRO){@($env:FIN_DEMO_DISTRO)}else{@(wsl.exe --list --quiet) -replace [char]0,''}; $chosen=$null; foreach($candidate in $ds){$candidate=$candidate.Trim(); if(!$candidate){continue}; wsl.exe -d $candidate --exec bash --version *> $null; if($LASTEXITCODE -ne 0){continue}; wsl.exe -d $candidate --exec python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 2)' *> $null; if($LASTEXITCODE -eq 0){$chosen=$candidate; break}}; if(!$chosen){[Console]::Error.WriteLine('No WSL distribution with bash and Python 3.12+ found. Use the no-install results ZIP or set FIN_DEMO_DISTRO.'); exit 2}; $ErrorActionPreference='Stop'; Write-Host ('WSL: '+$chosen); $unix=(wsl.exe -d $chosen --exec wslpath -u $env:FIN_DEMO_SOURCE | Out-String).Trim(); if($LASTEXITCODE -ne 0){exit 2}; wsl.exe -d $chosen --exec bash ($unix+'/scripts/wsl_demo.sh') $env:FIN_DEMO_SOURCE; exit $LASTEXITCODE"
if errorlevel 1 (
  echo Demo failed. Read the error above. No-install results are available in the Release.
  if not "%FIN_DEMO_NO_OPEN%"=="1" pause
  exit /b 1
)
if not "%FIN_DEMO_NO_OPEN%"=="1" start "" "%~dp0data\windows-demo\index.html"
echo Results: "%~dp0data\windows-demo\index.html"
if not "%FIN_DEMO_NO_OPEN%"=="1" pause
