@echo off
cd /d "%~dp0"
set "URL=https://longtan-zhice-73b61.containers.snapdeploy.app"

echo ========================================
echo  LongTan ZhiCe - open public demo
echo ========================================
echo.
echo This opens the public HTTPS demo in your browser.
echo It does NOT start the local server, and it does not
echo read or write your local enterprise database.
echo For offline demo on this PC, use the other one-click
echo script in this folder (http://127.0.0.1:8000/).
echo.
echo The free instance may sleep after about 15 minutes idle.
echo The first open may take 60-90 seconds to wake.
echo Public demo data is separate from your local projects.
echo.

start "" "%URL%"
echo Browser should open:
echo %URL%
pause
exit /b 0
