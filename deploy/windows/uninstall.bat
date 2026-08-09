@echo off
rem ===========================================================================
rem  Distribution ERP - remove the service from this PC
rem ===========================================================================
rem  Run as Administrator.
rem
rem  This removes the parts that make the ERP run:
rem      - the Windows service (and the fallback startup task)
rem      - the firewall rule that let other PCs connect
rem      - the nightly backup task
rem
rem  IT DOES NOT DELETE ANY DATA. All of this is left exactly as it is:
rem      C:\erp\data\erp.sqlite3      the database - every bill ever entered
rem      C:\erp\data\backups\         every backup taken
rem      C:\erp\media\                the uploaded company logo
rem      C:\erp\logs\                 the log files
rem      C:\erp\.env                  the settings, including the secret key
rem
rem  So this is reversible: run install.bat again and the same data comes back.
rem
rem  To remove the ERP completely, run this first, then delete C:\erp by hand -
rem  but take a copy of C:\erp\data somewhere safe before you do, because that
rem  is the accounts and there is no other copy on this machine.
rem ===========================================================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\.." >nul
set "ERP_ROOT=%CD%"
popd >nul

set "NSSM=%SCRIPT_DIR%vendor\nssm.exe"
set "SERVICE_NAME=ERP"
set "FIREWALL_RULE=Distribution ERP (port 8000)"

echo.
echo ===========================================================================
echo   Distribution ERP - remove the service
echo ===========================================================================
echo.
echo   This stops the ERP and removes it from Windows startup.
echo.
echo   Your data is NOT deleted:
echo     %ERP_ROOT%\data      the database and every backup
echo     %ERP_ROOT%\media     the company logo
echo     %ERP_ROOT%\.env      the settings
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo   FAILED: run this as Administrator.
    echo   Right-click uninstall.bat and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

set "CONFIRM="
set /p "CONFIRM=Type  yes  and press Enter to continue: "
if /i not "%CONFIRM%"=="yes" (
    echo.
    echo   Nothing was changed.
    echo.
    pause
    exit /b 0
)

echo.
echo [1/4] Stopping and removing the service...
if exist "%NSSM%" (
    "%NSSM%" stop "%SERVICE_NAME%" >nul 2>&1
    "%NSSM%" remove "%SERVICE_NAME%" confirm >nul 2>&1
) else (
    net stop "%SERVICE_NAME%" >nul 2>&1
    sc delete "%SERVICE_NAME%" >nul 2>&1
)
rem The fallback startup task, in case this machine was installed without NSSM.
schtasks /End /TN "ERP Server" >nul 2>&1
schtasks /Delete /TN "ERP Server" /F >nul 2>&1
echo       Done.

echo.
echo [2/4] Removing the firewall rule...
netsh advfirewall firewall delete rule name="%FIREWALL_RULE%" >nul 2>&1
rem The name install.bat used before this rule was renamed; harmless if absent.
netsh advfirewall firewall delete rule name="ERP 8000" >nul 2>&1
echo       Done - other PCs can no longer connect.

echo.
echo [3/4] Removing the nightly backup task...
schtasks /Delete /TN "ERP Nightly Backup" /F >nul 2>&1
echo       Done.
echo.
echo       NOTE: nothing will back this database up any more. If you are
echo             keeping the data, copy %ERP_ROOT%\data somewhere safe.

echo.
echo [4/4] Checking nothing is still listening on port 8000...
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo       Done - port 8000 is free.
) else (
    echo       WARNING: something is still listening on port 8000.
    echo       If the ERP was also started by hand in a black command window,
    echo       close that window.
)

echo.
echo ===========================================================================
echo   REMOVED.
echo ===========================================================================
echo.
echo   The ERP no longer starts with Windows and other PCs cannot reach it.
echo.
echo   Still on this PC, untouched:
echo     %ERP_ROOT%\data\erp.sqlite3
echo     %ERP_ROOT%\data\backups\
echo     %ERP_ROOT%\media\
echo     %ERP_ROOT%\logs\
echo     %ERP_ROOT%\.env
echo.
echo   To put it back: run install.bat as Administrator. The same data returns.
echo.
pause
exit /b 0
