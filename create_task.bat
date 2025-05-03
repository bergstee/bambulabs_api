@echo off
schtasks /create /tn MonitorPrinters /tr "'C:\Users\steph\AppData\Local\Programs\Python\Python312\python.exe' 'c:\bambulabs_api\examples\monitor_printers.py'" /sc ONSTART /ru SYSTEM /rl HIGHEST /f /wd "c:\bambulabs_api\examples"
if %errorlevel% neq 0 (
  echo Failed to create scheduled task. Errorlevel: %errorlevel%
  exit /b %errorlevel%
) else (
  echo Successfully created scheduled task 'MonitorPrinters'.
)
