@echo off
rem ===========================================================================
rem  Distribution ERP - install a new version
rem ===========================================================================
rem  Usage, as Administrator:
rem
rem      C:\erp\deploy\windows\update.bat C:\erp-new
rem
rem  where C:\erp-new is the folder you unzipped the NEW release into.
rem
rem  What it does, in this order, and it stops at the first failure:
rem
rem      1. checks it is Administrator and that the new folder looks right
rem      2. stops the ERP  (nobody can be typing a bill while files move)
rem      3. takes a backup  (this is the undo)
rem      4. copies the current program files aside, then copies the new ones in
rem      5. installs any new packages, from the new release's wheels folder
rem      6. migrates the database and collects the web pages
rem      7. starts the ERP again and checks it answers
rem
rem  YOUR DATA IS NOT TOUCHED. The database, the uploaded logo, the settings
rem  file and every backup stay exactly where they are - only program files
rem  are replaced.
rem
rem  If step 7 fails, roll back: see TROUBLESHOOTING.md, "Undoing an update".
rem ===========================================================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\.." >nul
set "ERP_ROOT=%CD%"
popd >nul

set "NEW=%~1"
set "VENV=%ERP_ROOT%\.venv"
set "PYEXE=%VENV%\Scripts\python.exe"
set "NSSM=%SCRIPT_DIR%vendor\nssm.exe"
set "SERVICE_NAME=ERP"

rem A timestamped folder, so two updates in one day do not overwrite each
rem other's rollback copy.
for /f "delims=" %%t in ('%PYEXE% -c "import datetime;print(datetime.datetime.now().strftime('%%Y%%m%%d-%%H%%M%%S'))" 2^>nul') do set "STAMP=%%t"
if not defined STAMP set "STAMP=manual"
set "ROLLBACK=%ERP_ROOT%\.rollback\%STAMP%"

echo.
echo ===========================================================================
echo   Distribution ERP - update
echo ===========================================================================
echo   Installed at : %ERP_ROOT%
echo   New version  : %NEW%
echo   Rollback copy: %ROLLBACK%
echo.

rem ------------------------------------------------------------- 1. checks
echo [1/7] Checking...
net session >nul 2>&1
if errorlevel 1 (
    echo   FAILED: run this as Administrator.
    echo   Right-click update.bat and choose "Run as administrator".
    goto :failed
)

if "%NEW%"=="" (
    echo   FAILED: you did not say where the new version is.
    echo.
    echo   Unzip the new release somewhere first, then run:
    echo       %~f0 C:\erp-new
    echo.
    goto :failed
)

if not exist "%NEW%\manage.py" (
    echo   FAILED: %NEW% does not look like an ERP release.
    echo   There is no manage.py in it. Check the path, and make sure you
    echo   unzipped the file rather than opening it inside the zip viewer.
    goto :failed
)

if not exist "%PYEXE%" (
    echo   FAILED: there is no installation at %ERP_ROOT% to update.
    echo   Run install.bat instead.
    goto :failed
)

for /f "delims=" %%v in ('type "%NEW%\deploy\windows\VERSION.txt" 2^>nul') do set "NEWVER=%%v"
if defined NEWVER echo       New version is %NEWVER%
echo       OK.

rem -------------------------------------------------------------- 2. stop
echo.
echo [2/7] Stopping the ERP...
call :stop_service
echo       OK - stopped.

rem ------------------------------------------------------------ 3. backup
echo.
echo [3/7] Taking a backup first...
pushd "%ERP_ROOT%"
"%PYEXE%" manage.py backup --no-usb --settings=config.settings.prod
set "BACKUP_RC=%errorlevel%"
popd
if "%BACKUP_RC%"=="2" (
    echo.
    echo   FAILED: no backup could be taken, so the update is stopping here.
    echo   Updating without one means a problem could not be undone.
    echo   Fix the backup first - see TROUBLESHOOTING.md, "The backup fails".
    call :start_service
    goto :failed
)
echo       OK - backup taken.

rem -------------------------------------------------------------- 4. files
echo.
echo [4/7] Replacing the program files...
mkdir "%ROLLBACK%" 2>nul

rem Program folders only. data, media, logs, .env, .venv and .rollback are
rem deliberately absent from both lists: they are this installation, not this
rem version of the software.
for %%D in (apps config templates static deploy) do (
    if exist "%ERP_ROOT%\%%D" (
        robocopy "%ERP_ROOT%\%%D" "%ROLLBACK%\%%D" /E /NFL /NDL /NJH /NJS /NP >nul
    )
)
for %%F in (manage.py serve.py requirements.txt CLAUDE.md) do (
    if exist "%ERP_ROOT%\%%F" copy /Y "%ERP_ROOT%\%%F" "%ROLLBACK%\" >nul
)
echo       Current version copied to %ROLLBACK%

rem /MIR mirrors, so a file deleted in the new release is deleted here too -
rem which matters, because a stale .py left behind can still be imported and
rem shadow the new one.
for %%D in (apps config templates static deploy) do (
    if exist "%NEW%\%%D" (
        robocopy "%NEW%\%%D" "%ERP_ROOT%\%%D" /MIR /NFL /NDL /NJH /NJS /NP >nul
        if errorlevel 8 (
            echo   FAILED: could not replace the %%D folder.
            echo   Something still has a file open. Is the ERP really stopped?
            goto :failed
        )
    )
)
for %%F in (manage.py serve.py requirements.txt CLAUDE.md) do (
    if exist "%NEW%\%%F" copy /Y "%NEW%\%%F" "%ERP_ROOT%\" >nul
)
echo       OK - new files in place.

rem ----------------------------------------------------------- 5. packages
echo.
echo [5/7] Installing any new packages...
if exist "%NEW%\deploy\windows\wheels" (
    "%PYEXE%" -m pip install --no-index --find-links="%NEW%\deploy\windows\wheels" -r "%ERP_ROOT%\requirements.txt"
    if errorlevel 1 (
        echo   FAILED: the packages could not be installed.
        echo   Roll back - see TROUBLESHOOTING.md, "Undoing an update".
        goto :failed
    )
    echo       OK.
) else (
    echo       No wheels folder in the new release - skipping.
)

rem ---------------------------------------------------- 6. migrate+static
echo.
echo [6/7] Updating the database and the web pages...
pushd "%ERP_ROOT%"
"%PYEXE%" manage.py migrate --noinput --settings=config.settings.prod
if errorlevel 1 (
    echo   FAILED: the database could not be updated.
    echo   Roll back - see TROUBLESHOOTING.md, "Undoing an update".
    popd
    goto :failed
)
"%PYEXE%" manage.py collectstatic --noinput --clear --settings=config.settings.prod >nul
if errorlevel 1 (
    echo   FAILED: the web page files could not be collected.
    popd
    goto :failed
)
popd
echo       OK.

rem -------------------------------------------------------------- 7. start
echo.
echo [7/7] Starting the ERP and checking it answers...
call :start_service

pushd "%ERP_ROOT%"
"%PYEXE%" manage.py preflight --service --settings=config.settings.prod
set "PREFLIGHT=%errorlevel%"
popd

if not "%PREFLIGHT%"=="0" (
    echo.
    echo ===========================================================================
    echo   THE UPDATE DID NOT COME UP CLEANLY
    echo ===========================================================================
    echo.
    echo   The previous version is saved at:
    echo       %ROLLBACK%
    echo.
    echo   To go back, see TROUBLESHOOTING.md, "Undoing an update".
    echo.
    goto :failed
)

echo.
echo ===========================================================================
echo   UPDATED.
echo ===========================================================================
if defined NEWVER echo   Now running version %NEWVER%.
echo.
echo   The previous version is kept at %ROLLBACK% in case it is needed.
echo   It is safe to delete once the office has used the new one for a day.
echo.
pause
exit /b 0

rem ===========================================================================
:stop_service
if exist "%NSSM%" (
    "%NSSM%" stop "%SERVICE_NAME%" >nul 2>&1
) else (
    net stop "%SERVICE_NAME%" >nul 2>&1
    schtasks /End /TN "ERP Server" >nul 2>&1
    taskkill /F /IM python.exe /FI "WINDOWTITLE eq ERP*" >nul 2>&1
)
rem Windows releases file handles a moment after the process exits; copying too
rem soon fails with "being used by another process".
ping -n 4 127.0.0.1 >nul
exit /b 0

:start_service
if exist "%NSSM%" (
    "%NSSM%" start "%SERVICE_NAME%" >nul 2>&1
) else (
    net start "%SERVICE_NAME%" >nul 2>&1
    schtasks /Run /TN "ERP Server" >nul 2>&1
)
exit /b 0

rem ===========================================================================
:failed
echo.
echo   The update stopped. Read the message above.
echo.
pause
exit /b 1
