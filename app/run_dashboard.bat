@echo off
REM ETF bot local dashboard — keep this window open while using the site.
cd /d "%~dp0"
echo.
echo  Opening dashboard at:  http://127.0.0.1:5050
echo  Use http:// not https://   (same machine only)
echo  Close this window or press Ctrl+C to stop the dashboard.
echo.
python dashboard_app.py
if errorlevel 1 pause
