@echo off
cd /d "%~dp0"
set "BACKEND=%~dp0backend"
set "PY=%BACKEND%\.venv\Scripts\python.exe"
set "URL=http://127.0.0.1:8000/"
set "NEED_VER=8.2.9"
set "NEED_BUILD=v829-audit-20261111"

echo ========================================
echo  LongTan ZhiCe - one click start
echo ========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo Python not found. Install Python 3.10+ and check Add to PATH.
  pause
  exit /b 1
)

if not exist "%BACKEND%\requirements.txt" (
  echo backend folder missing: %BACKEND%
  pause
  exit /b 1
)

cd /d "%BACKEND%"

if not exist "%PY%" (
  echo [1/4] create venv...
  python -m venv .venv
  if errorlevel 1 (
    echo venv failed
    pause
    exit /b 1
  )
)

if not exist "%PY%" (
  echo venv python missing: %PY%
  pause
  exit /b 1
)

echo [2/4] install deps...
"%PY%" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo pip failed
  pause
  exit /b 1
)

echo [3/4] import check...
"%PY%" -c "from app.main import app; print('ok')"
if errorlevel 1 (
  echo backend import failed - see error above
  pause
  exit /b 1
)

echo [4/4] starting server...
echo Keep the backend window open. URL: %URL%
echo Expected health version: %NEED_VER%  build: %NEED_BUILD%
echo Data stays on this PC (backend\data). Free Render will not keep enterprise data after sleep or redeploy.
echo.

netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if not errorlevel 1 (
  powershell -NoProfile -Command "try { $j=Invoke-RestMethod -Uri http://127.0.0.1:8000/api/health -TimeoutSec 2; if($j.ok -and $j.version -eq '%NEED_VER%' -and ((-not $env:NEED_BUILD) -or $j.build_id -eq '%NEED_BUILD%')){exit 0}else{Write-Host ('port 8000 is running version [' + $j.version + '] build [' + $j.build_id + '], need %NEED_VER% / %NEED_BUILD%. Close the old backend window and retry.'); exit 2} } catch { Write-Host 'port 8000 is listening but /api/health failed'; exit 1 }"
  if errorlevel 1 (
    pause
    exit /b 1
  )
  echo port 8000 already running with version %NEED_VER%, open browser
  start "" "%URL%"
  pause
  exit /b 0
)

start "LongTan-Backend" /D "%BACKEND%" cmd /k ""%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

set /a n=0
:waitloop
set /a n+=1
powershell -NoProfile -Command "try { $j=Invoke-RestMethod -Uri http://127.0.0.1:8000/api/health -TimeoutSec 1; if($j.ok -and $j.version -eq '%NEED_VER%'){exit 0}else{exit 1} } catch { exit 1 }"
if not errorlevel 1 goto ready
if %n% GEQ 45 goto fail
timeout /t 1 /nobreak >nul
goto waitloop

:ready
echo ready, version %NEED_VER% build %NEED_BUILD%, opening browser...
start "" "%URL%"
echo Backend window title: LongTan-Backend - do not close it.
pause
exit /b 0

:fail
echo timeout or unexpected version - check LongTan-Backend window for errors
start "" "%URL%"
pause
exit /b 1
