@echo off
rem ===========================================================================
rem  Distribution ERP - install on this Windows PC
rem ===========================================================================
rem  Run this ONCE, as Administrator, after unzipping the release to C:\erp.
rem  Right-click install.bat -> "Run as administrator".
rem
rem  It does nine things and stops at the first one that fails:
rem
rem     1. checks it is running as Administrator
rem     2. checks Python 3.12 is installed and on the PATH
rem     3. creates the virtual environment in C:\erp\.venv
rem     4. installs the dependencies from the bundled wheels folder (offline)
rem     5. writes C:\erp\.env with a freshly generated SECRET_KEY
rem     6. creates the database and collects the static files
rem     7. asks for the first administrator login
rem     8. installs the ERP as a Windows service and starts it
rem     9. opens port 8000 on the firewall and checks the whole thing answers
rem
rem  It is safe to run again: an existing .env is kept, an existing database is
rem  migrated rather than replaced, and an existing service is reinstalled.
rem ===========================================================================

setlocal enabledelayedexpansion

rem The folder this script is in is deploy\windows, so the app root is two up.
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\.." >nul
set "ERP_ROOT=%CD%"
popd >nul

set "VENV=%ERP_ROOT%\.venv"
set "PYEXE=%VENV%\Scripts\python.exe"
set "WHEELS=%SCRIPT_DIR%wheels"
set "NSSM=%SCRIPT_DIR%vendor\nssm.exe"
set "SERVICE_NAME=ERP"
set "FIREWALL_RULE=Distribution ERP (port 8000)"
set "DJANGO_SETTINGS_MODULE=config.settings.prod"

echo.
echo ===========================================================================
echo   Distribution ERP - installation
echo ===========================================================================
echo   Installing into: %ERP_ROOT%
echo.

rem ---------------------------------------------------------------- 1. admin
rem The service and the firewall rule both need it, and finding that out in
rem step 8 after twenty minutes of installing is the wrong time.
echo [1/9] Checking for Administrator rights...
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   FAILED: this must be run as Administrator.
    echo.
    echo   Close this window. Find install.bat in Windows Explorer,
    echo   right-click it, and choose "Run as administrator".
    echo.
    goto :failed
)
echo       OK - running as Administrator.

rem A space in the path breaks the service registration, the scheduled task and
rem several of the commands below, in ways that only show up much later. Caught
rem here, where the fix is to move the folder, rather than at 21:00 in a backup
rem task that silently does nothing.
echo "%ERP_ROOT%" | findstr /C:" " >nul
if not errorlevel 1 (
    echo.
    echo   FAILED: the path has a space in it:
    echo       %ERP_ROOT%
    echo.
    echo   Move the folder to C:\erp and run this again. Every instruction in
    echo   INSTALL-WINDOWS.md and TROUBLESHOOTING.md assumes that path.
    echo.
    goto :failed
)

rem --------------------------------------------------------------- 2. python
echo.
echo [2/9] Checking for Python 3.12...

set "SYSPY="
rem The py launcher is what the python.org installer puts on the PATH, and it
rem can be asked for 3.12 specifically. Fall back to a bare "python" for an
rem install that skipped the launcher.
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "SYSPY=py -3.12"
if not defined SYSPY (
    python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>&1
    if not errorlevel 1 set "SYSPY=python"
)

if not defined SYSPY (
    echo.
    echo   FAILED: Python 3.12 was not found.
    echo.
    echo   What to do:
    echo     1. Close this window.
    echo     2. Open the "python" folder next to this script.
    echo     3. Run the installer in it.
    echo     4. TICK THE BOX "Add python.exe to PATH" on the first screen.
    echo        This is the step everybody misses. Without it, nothing below works.
    echo     5. Finish the installer, then run install.bat again.
    echo.
    echo   If Python IS installed, it is the wrong version. This application
    echo   needs 3.12 exactly. Check with:   py -0
    echo.
    goto :failed
)

for /f "delims=" %%v in ('%SYSPY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo       OK - found Python %PYVER% (using: %SYSPY%)

rem ----------------------------------------------------------------- 3. venv
echo.
echo [3/9] Creating the virtual environment...
if exist "%PYEXE%" (
    echo       Already there - reusing %VENV%
) else (
    %SYSPY% -m venv "%VENV%"
    if errorlevel 1 (
        echo   FAILED: could not create the virtual environment in %VENV%
        echo   Check there is free disk space and that antivirus is not blocking it.
        goto :failed
    )
    echo       OK - created %VENV%
)

rem ------------------------------------------------------------- 4. packages
echo.
echo [4/9] Installing the application's packages...
if not exist "%WHEELS%" (
    echo   FAILED: the wheels folder is missing: %WHEELS%
    echo   The zip did not unpack completely. Unzip it again.
    goto :failed
)

rem --no-index is what makes this work with no internet: pip is forbidden from
rem contacting PyPI and must satisfy everything from the bundled folder. If a
rem wheel is missing this fails here, loudly, rather than hanging on a network
rem timeout for two minutes per package.
"%PYEXE%" -m pip install --no-index --find-links="%WHEELS%" --upgrade pip >nul 2>&1
"%PYEXE%" -m pip install --no-index --find-links="%WHEELS%" -r "%ERP_ROOT%\requirements.txt"
if errorlevel 1 (
    echo.
    echo   FAILED: the packages could not be installed.
    echo.
    echo   This is almost always one of two things:
    echo     - the zip did not unpack completely; unzip it again, or
    echo     - the wrong Python version; this needs 3.12 (found %PYVER%).
    echo.
    goto :failed
)
echo       OK - packages installed.

rem ------------------------------------------------------------------ 5. env
echo.
echo [5/9] Writing the settings file...
"%PYEXE%" "%SCRIPT_DIR%bootstrap_env.py" "%ERP_ROOT%"
if errorlevel 1 (
    echo   FAILED: could not write %ERP_ROOT%\.env
    goto :failed
)

rem ------------------------------------------------------- 6. database + css
echo.
echo [6/9] Setting up the database and the web pages...
pushd "%ERP_ROOT%"

"%PYEXE%" manage.py migrate --noinput --settings=config.settings.prod
if errorlevel 1 (
    echo   FAILED: the database could not be created.
    echo   See TROUBLESHOOTING.md, "database is locked".
    popd
    goto :failed
)

"%PYEXE%" manage.py collectstatic --noinput --settings=config.settings.prod >nul
if errorlevel 1 (
    echo   FAILED: the web page files could not be collected.
    popd
    goto :failed
)
echo       OK - database ready, web pages collected.

rem ------------------------------------------------------------ 7. superuser
echo.
echo [7/9] Creating the first administrator login.
echo.
echo       You will be asked for a username, an e-mail and a password.
echo       - The e-mail can be left blank; press Enter.
echo       - The password will NOT appear on screen as you type. That is normal.
echo       - Write down what you choose. Nobody can recover it for you.
echo.

rem Skipped when there is already a superuser, so re-running the installer does
rem not stop to ask a question that has been answered.
"%PYEXE%" -c "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.prod');django.setup();from django.contrib.auth import get_user_model;raise SystemExit(0 if get_user_model().objects.filter(is_superuser=True).exists() else 1)" >nul 2>&1
if not errorlevel 1 (
    echo       An administrator login already exists - skipping.
) else (
    "%PYEXE%" manage.py createsuperuser --settings=config.settings.prod
    if errorlevel 1 (
        echo.
        echo   The login was not created. You can do it later with:
        echo       cd %ERP_ROOT%
        echo       .venv\Scripts\python.exe manage.py createsuperuser --settings=config.settings.prod
        echo.
    )
)
popd

rem -------------------------------------------------------------- 8. service
echo.
echo [8/9] Installing the ERP as a Windows service...

if exist "%NSSM%" (
    call :install_with_nssm
) else (
    echo       nssm.exe was not in the zip - using a scheduled task instead.
    call :install_with_task
)
if errorlevel 1 goto :failed

rem ------------------------------------------------------- 9. firewall+check
echo.
echo [9/9] Opening the firewall and checking it all works...

rem Deleted first so a re-run does not stack duplicate rules with the same name.
netsh advfirewall firewall delete rule name="%FIREWALL_RULE%" >nul 2>&1
rem remoteip=LocalSubnet keeps this to the office LAN. profile=private,domain
rem leaves it closed on a public network, which matters if this is ever a
rem laptop on a hotel wifi.
netsh advfirewall firewall add rule name="%FIREWALL_RULE%" dir=in action=allow protocol=TCP localport=8000 remoteip=LocalSubnet profile=private,domain >nul
if errorlevel 1 (
    echo       WARNING: the firewall rule could not be added.
    echo       This PC will work, but other PCs will not be able to connect.
    echo       See TROUBLESHOOTING.md, "Other PCs cannot connect".
) else (
    echo       OK - port 8000 is open to the local network.
)

echo.
echo       Waiting for the ERP to answer...
pushd "%ERP_ROOT%"
"%PYEXE%" manage.py preflight --service --settings=config.settings.prod
set "PREFLIGHT=%errorlevel%"
popd
if not "%PREFLIGHT%"=="0" (
    echo.
    echo   The installation finished but the checks above did not all pass.
    echo   Fix what they list, then run this to check again:
    echo       cd %ERP_ROOT%
    echo       .venv\Scripts\python.exe manage.py preflight --settings=config.settings.prod
    echo.
    goto :failed
)

rem ---------------------------------------------------------------- finished
rem The same address bootstrap_env.py detected, asked for again so the banner
rem shows it even when .env was already there from a previous run.
for /f "delims=" %%i in ('"%PYEXE%" "%SCRIPT_DIR%bootstrap_env.py" --print-lan-address 2^>nul') do set "LANIP=%%i"

echo.
echo ===========================================================================
echo   INSTALLED.
echo ===========================================================================
echo.
echo   On THIS PC, open:        http://localhost:8000
if defined LANIP (
echo   On the OTHER PCs, open:  http://%LANIP%:8000
echo.
echo   ^>^>^> Write this address down: http://%LANIP%:8000
echo        Every other computer in the office types that into its browser.
) else (
echo.
echo   The LAN address could not be detected. Run  ipconfig  and look for
echo   "IPv4 Address". Other PCs use http://THAT-ADDRESS:8000
)
echo.
echo   The ERP starts by itself whenever this PC is switched on.
echo.
echo   Next, in INSTALL-WINDOWS.md:
echo     - step 6:  log in and change the administrator password
echo     - step 8:  set up the Google Drive backup
echo     - step 9:  import the nightly backup task
echo     - step 10: the checks that prove it works
echo.
pause
exit /b 0

rem ===========================================================================
rem  Service installation, two ways
rem ===========================================================================
:install_with_nssm
rem NSSM exists because a plain Python process cannot be a Windows service on
rem its own: the Service Control Manager expects the program to answer its
rem start/stop protocol, and waitress does not. NSSM is that shim.
"%NSSM%" stop "%SERVICE_NAME%" >nul 2>&1
"%NSSM%" remove "%SERVICE_NAME%" confirm >nul 2>&1

"%NSSM%" install "%SERVICE_NAME%" "%PYEXE%" "%ERP_ROOT%\serve.py"
if errorlevel 1 (
    echo   FAILED: could not install the service.
    exit /b 1
)
"%NSSM%" set "%SERVICE_NAME%" DisplayName "Distribution ERP" >nul
"%NSSM%" set "%SERVICE_NAME%" Description "Sales, purchasing, stock and accounts for the distribution business. Serves http://localhost:8000" >nul
"%NSSM%" set "%SERVICE_NAME%" AppDirectory "%ERP_ROOT%" >nul
"%NSSM%" set "%SERVICE_NAME%" AppEnvironmentExtra DJANGO_SETTINGS_MODULE=config.settings.prod >nul
rem Auto-start: the whole point is that nobody has to remember to start it after
rem a power cut.
"%NSSM%" set "%SERVICE_NAME%" Start SERVICE_AUTO_START >nul
rem Anything the process prints goes to a file, rotated at 5 MB, because this
rem machine runs unattended for months.
"%NSSM%" set "%SERVICE_NAME%" AppStdout "%ERP_ROOT%\logs\service-out.log" >nul
"%NSSM%" set "%SERVICE_NAME%" AppStderr "%ERP_ROOT%\logs\service-err.log" >nul
"%NSSM%" set "%SERVICE_NAME%" AppRotateFiles 1 >nul
"%NSSM%" set "%SERVICE_NAME%" AppRotateOnline 1 >nul
"%NSSM%" set "%SERVICE_NAME%" AppRotateBytes 5242880 >nul
rem Restart if it ever dies, but back off - a crash loop that restarts instantly
rem fills the disk with logs faster than anybody notices the ERP is down.
"%NSSM%" set "%SERVICE_NAME%" AppExit Default Restart >nul
"%NSSM%" set "%SERVICE_NAME%" AppRestartDelay 5000 >nul
rem Give waitress time to finish serving whatever it is holding before Windows
rem kills it during a shutdown or an update.
"%NSSM%" set "%SERVICE_NAME%" AppStopMethodConsole 15000 >nul

if not exist "%ERP_ROOT%\logs" mkdir "%ERP_ROOT%\logs"

"%NSSM%" start "%SERVICE_NAME%"
if errorlevel 1 (
    echo   FAILED: the service was installed but would not start.
    echo   Look in %ERP_ROOT%\logs\service-err.log for the reason.
    exit /b 1
)
echo       OK - service "%SERVICE_NAME%" installed and started, set to auto-start.
exit /b 0

:install_with_task
rem Fallback for a release built without the NSSM binary. A scheduled task that
rem triggers at boot is not as good as a service - it does not restart if the
rem process dies - but it does start the ERP without anybody logging in, which
rem is the property that matters.
schtasks /Delete /TN "ERP Server" /F >nul 2>&1
schtasks /Create /TN "ERP Server" /TR "\"%PYEXE%\" \"%ERP_ROOT%\serve.py\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F >nul
if errorlevel 1 (
    echo   FAILED: could not create the startup task.
    exit /b 1
)
schtasks /Run /TN "ERP Server" >nul
echo       OK - startup task "ERP Server" created and started.
echo       NOTE: this is the fallback. A proper service is better - see
echo             TROUBLESHOOTING.md, "The service will not start".
exit /b 0

rem ===========================================================================
:failed
echo.
echo ===========================================================================
echo   INSTALLATION DID NOT FINISH
echo ===========================================================================
echo.
echo   Nothing has been damaged. Read the message above, fix it, and run
echo   install.bat again - it is safe to run more than once.
echo.
echo   If you are stuck, open TROUBLESHOOTING.md in this folder.
echo.
pause
exit /b 1
