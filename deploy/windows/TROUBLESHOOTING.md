# When something is wrong

Print this and keep it with the PC.

**Start here, whatever the problem is.** Nine times in ten this tells you the
answer, and it changes nothing:

1. Press the **Windows key**, type `cmd`, press **Enter**.
2. Copy these two lines in, one at a time, pressing Enter after each:

   ```
   cd C:\erp
   .venv\Scripts\python.exe manage.py preflight --settings=config.settings.prod
   ```

Every line it prints starts with `OK`, `WARN` or `FAIL`. A `FAIL` line says what
is wrong and what to type to fix it. Do that first, then come back here only if
it did not help.

**The two files worth looking in**, newest lines at the bottom:

| File | What is in it |
| ---- | ------------- |
| `C:\erp\logs\erp.log` | the application — errors from pages, backups, postings |
| `C:\erp\logs\service-err.log` | the service — reasons it would not start |

Open them by double-clicking; they are plain text. If Windows asks what to open
them with, choose **Notepad**.

---

## Contents

1. [The service will not start](#1-the-service-will-not-start)
2. [Other PCs cannot connect](#2-other-pcs-cannot-connect)
3. [The printer does not appear](#3-the-printer-does-not-appear)
4. ["Database is locked"](#4-database-is-locked)
5. [The backup fails](#5-the-backup-fails)
6. [Undoing an update](#6-undoing-an-update)
7. [Python is the wrong version](#7-python-is-the-wrong-version)
8. [Nobody can log in](#8-nobody-can-log-in)
9. [The pages look like plain text with no colours](#9-the-pages-look-like-plain-text-with-no-colours)
10. [Getting a backup back](#10-getting-a-backup-back)

---

## 1. The service will not start

**Symptom:** `http://localhost:8000` says *"This site can't be reached"*, or the
ERP was working and has stopped.

### Step 1 — Is it running?

Open a command window and type:

```
sc query ERP
```

Look at the `STATE` line:

- **`4  RUNNING`** — it is running. The problem is not this; go to
  [section 2](#2-other-pcs-cannot-connect) if it is only the other PCs, or read
  `C:\erp\logs\erp.log`.
- **`1  STOPPED`** — go to step 2.
- **`The specified service does not exist`** — the service was never installed,
  or this PC uses the fallback startup task. Try:

  ```
  schtasks /Query /TN "ERP Server"
  ```

  If that also says it does not exist, run `install.bat` again as administrator.

### Step 2 — Start it and see what it says

```
net start ERP
```

If it starts, you are done — but find out **why it stopped**: open
`C:\erp\logs\service-err.log` and read the last few lines.

If it refuses, the real error is in the log. Open:

```
C:\erp\logs\service-err.log
```

and read the **last** paragraph. The common ones:

| The log says | What it means | Fix |
| ------------ | ------------- | --- |
| `Only one usage of each socket address` | Something else is already on port 8000 | Step 3 below |
| `ImproperlyConfigured: Set the SECRET_KEY` | `C:\erp\.env` is missing or empty | Step 4 below |
| `ALLOWED_HOSTS is empty` | Same — `.env` is damaged | Step 4 below |
| `No module named 'django'` | The virtual environment is broken | Step 5 below |
| `Permission denied` / `Access is denied` | The service account cannot write to `C:\erp` | Step 6 below |

### Step 3 — Something else is using port 8000

Find out what:

```
netstat -ano | findstr ":8000"
```

The last number on the line is the process ID. Then:

```
tasklist /FI "PID eq 1234"
```

replacing `1234` with that number.

- If it is **`python.exe`**, the ERP is already running — somebody started it by
  hand in a black window. Close that window, or restart the PC.
- If it is something else (Skype and some development tools use 8000), either
  close that program, or move the ERP to another port:
  1. Open `C:\erp\.env` in Notepad, change `ERP_PORT=8000` to `ERP_PORT=8080`,
     save.
  2. Add the firewall rule for the new port (see
     [section 2](#2-other-pcs-cannot-connect), "Add the rule by hand", changing
     8000 to 8080).
  3. `net stop ERP` then `net start ERP`.
  4. **Tell everyone the address has changed** to `http://…:8080`.

### Step 4 — The .env file is missing or damaged

Check it exists and has something in it:

```
type C:\erp\.env
```

If the file is missing or the `SECRET_KEY=` line is blank, rebuild it:

```
cd C:\erp
.venv\Scripts\python.exe deploy\windows\bootstrap_env.py C:\erp
```

> If a `.env` file is still there but broken, rename it to `.env.broken` first —
> the command above will not overwrite an existing one.

**A new SECRET_KEY signs everybody out.** Everyone logs in again. Nothing else
is lost — no data depends on it.

Then `net start ERP`.

### Step 5 — The virtual environment is broken

Usually antivirus quarantined something, or the disk filled up. Rebuild it:

```
cd C:\erp
rmdir /S /Q .venv
deploy\windows\install.bat
```

Run that last line **as administrator** (right-click → Run as administrator). It
keeps your data and your settings and reinstalls only the program parts.

### Step 6 — Permissions

The service runs as `LocalSystem`, which normally has access to everything. If
`C:\erp` was copied from a USB stick it can inherit odd permissions. Fix:

```
icacls C:\erp /grant "SYSTEM:(OI)(CI)F" /T
icacls C:\erp /grant "Administrators:(OI)(CI)F" /T
```

Then `net start ERP`.

### Last resort — run it by hand to see the error in front of you

```
cd C:\erp
set DJANGO_SETTINGS_MODULE=config.settings.prod
.venv\Scripts\python.exe serve.py
```

The error prints in the window. Press **Ctrl+C** to stop it, then fix and use
`net start ERP` as normal. **Do not leave it running this way** — it stops when
the window closes or the person logs out.

---

## 2. Other PCs cannot connect

**Symptom:** the ERP works on the server PC at `http://localhost:8000`, but
another PC says *"This site can't be reached"* or *"took too long to respond"*.

Work through these in order. It is nearly always number 2 or number 3.

### 2.1 — Are you using the right address?

The other PCs must **not** use `localhost`. That means "this computer" and on
another PC it means that PC.

On the **server**, run:

```
ipconfig
```

Find **IPv4 Address**, e.g. `192.168.1.50`. The other PCs use:

```
http://192.168.1.50:8000
```

Note `http://` and not `https://`, and `:8000` on the end.

### 2.2 — Is the firewall rule there?

On the server:

```
netsh advfirewall firewall show rule name="Distribution ERP (port 8000)"
```

If it says *"No rules match"*, add it. Open a command window
**as administrator** and paste:

```
netsh advfirewall firewall add rule name="Distribution ERP (port 8000)" dir=in action=allow protocol=TCP localport=8000 remoteip=LocalSubnet profile=private,domain
```

### 2.3 — Is the network set to "Private"?

The rule above is off on a network Windows thinks is *Public*. Offices often get
labelled Public by accident.

1. On the server: **Settings** → **Network & Internet**.
2. Click the connection in use (Wi-Fi or Ethernet), then **Properties**.
3. Under **Network profile type**, choose **Private**.

*This is the single most common cause.*

### 2.4 — Can the other PC see the server at all?

On the **other** PC, in a command window:

```
ping 192.168.1.50
```

- **Replies** → the network is fine; the problem is the firewall or the address.
  Go back to 2.2.
- **"Request timed out"** or **"Destination host unreachable"** → the two PCs are
  not on the same network. Check they are on the same Wi-Fi / the same switch.
  A PC on a "Guest" Wi-Fi cannot reach one on the main network — that is what
  guest Wi-Fi is for.

### 2.5 — Does the address get refused with "Bad Request (400)"?

If the page loads but says **"Bad Request (400)"**, the server is running and
reachable — it just does not recognise the address it was reached on. Run the
preflight on the server:

```
cd C:\erp
.venv\Scripts\python.exe manage.py preflight --settings=config.settings.prod
```

It will print a `FAIL` line naming the address and exactly what to put in
`C:\erp\.env`. Make that change, then `net stop ERP` and `net start ERP`.

### 2.6 — It worked yesterday and not today

The router probably gave the server a different address. Run `ipconfig` on the
server and compare it with what the other PCs have bookmarked.

The permanent fix is a **DHCP reservation** — ask whoever looks after the router
to always give this PC the same address. It takes them two minutes.

---

## 3. The printer does not appear

**Symptom:** Ctrl+P opens the print dialog but the office printer is not in the
list, or printing does nothing.

**The ERP does not print.** The browser does. So this is a Windows and browser
problem, and the fix is never in the ERP.

### 3.1 — Is the printer installed on the PC doing the printing?

Printing happens on **whichever PC has the browser open**, not on the server. If
a counter PC prints invoices, the printer must be installed on that counter PC.

1. **Settings** → **Bluetooth & devices** → **Printers & scanners**.
2. Is the printer listed? If not, click **Add device** and let Windows find it.
3. Print a Windows test page: click the printer → **Printer properties** →
   **Print Test Page**. **If the test page does not come out, the ERP is not the
   problem** — fix the printer in Windows first.

### 3.2 — The dialog opens but the list is short

Click **"See more"** or the **Destination** dropdown at the top of the print
dialog. Chrome and Edge show three printers and hide the rest behind that.

### 3.3 — It prints but looks wrong

- **No colours, no lines:** in the print dialog open **More settings** and tick
  **Background graphics**.
- **Cut off at the right:** set **Scale** to **Fit to printable area**, or set
  **Paper size** to **A4**.
- **A blank first page:** set **Margins** to **Default**, not None.

### 3.4 — Use the PDF instead

Every invoice, receipt and report has a **PDF** button beside the print button.
The PDF is generated by the ERP itself, so it looks identical everywhere, on
every printer. If browser printing keeps misbehaving, use it: click **PDF**,
then print the downloaded file from Adobe Reader or the browser's PDF viewer.

For the thermal till-roll printer at the counter, the receipt PDF is already
sized for it — see the `RECEIPT_LAYOUT` line in `C:\erp\.env` (`80mm`, `58mm`,
`a5` or `a4`).

---

## 4. "Database is locked"

**Symptom:** a red message saying `database is locked`, or
`OperationalError: database is locked` in `C:\erp\logs\erp.log`.

**What it means:** two things tried to write to the accounts at the same moment
and one waited 20 seconds without getting in. It is almost always a sign that
something else has hold of the file, not that the ERP is overloaded.

### 4.1 — Is anything else touching the database?

In order of how often it is the cause:

1. **A cloud sync folder.** If `C:\erp` is inside OneDrive, Dropbox or Google
   Drive, that program opens the database file to upload it and takes the lock.
   **Move the ERP out of the synced folder** — this is not a settings problem
   and no setting fixes it. The backups already go offsite; the live database
   must not be synced.
2. **Antivirus scanning it.** Add an exclusion for the folder `C:\erp\data`.
   In Windows Security: **Virus & threat protection** → **Manage settings** →
   **Exclusions** → **Add an exclusion** → **Folder** → `C:\erp\data`.
3. **A second copy of the ERP running.** See
   [section 1, step 3](#step-3--something-else-is-using-port-8000).
4. **A backup or restore running at the same time.** The nightly backup takes a
   read lock for a few seconds. If somebody is posting a bill at exactly 21:00
   they may see this once. It is harmless — they press Post again.
5. **DB Browser for SQLite, or any tool, with the file open.** Close it.

### 4.2 — Clear a stale lock

If nothing is holding it and it still complains, there is a leftover lock file.
**Stop the ERP first** — this is not safe while it is running:

```
net stop ERP
dir C:\erp\data
```

If you see `erp.sqlite3-wal` and `erp.sqlite3-shm`, that is **normal** and they
must **not** be deleted while the ERP runs. With it stopped, SQLite folds them
back in on the next start:

```
net start ERP
```

### 4.3 — If it happens every day

Something is structurally wrong — usually 4.1.1 or 4.1.2. Check
`C:\erp\logs\erp.log` for the time of day it happens; if it is always 21:00 it
is the backup, and if it is always the same minute each morning it is a scan.

---

## 5. The backup fails

**Symptom:** Task Scheduler shows `0x1` or `0x2` in **Last Run Result**, or the
Backup screen in the ERP is rust-coloured.

**First, know which half failed:**

| Code | Meaning | Urgency |
| ---- | ------- | ------- |
| `0x0` | Worked. An unplugged USB stick still counts as worked. | none |
| `0x1` | **The backup was taken.** A copy of it failed — nearly always Google Drive. | this week |
| `0x2` | **No backup was taken at all.** | today |

### 5.1 — See the actual reason

In the ERP, click **Backup** in the left menu. The history lists every attempt,
per destination, with the reason for each failure in plain words. That is the
fastest answer.

Or run one by hand and watch it:

```
cd C:\erp
.venv\Scripts\python.exe manage.py backup --push --settings=config.settings.prod
```

### 5.2 — `0x2`, no backup taken

Almost always disk space. Check the C: drive in Explorer — a backup needs about
as much free space as the database, twice over.

- Free space up, or
- Reduce how many are kept: in `C:\erp\.env` add
  `BACKUP_KEEP_DAILY=7` (from 14), then run a backup by hand — old ones are
  deleted at the end of each run.

If it is not space, the command in 5.1 prints the reason.

### 5.3 — `0x1`, Google Drive failed

Run the command in 5.1 and read the message. The common ones:

**"rclone is not installed"** — see INSTALL-WINDOWS.md step 8.1. If `rclone.exe`
is there but not found, add this line to `C:\erp\.env`:

```
BACKUP_RCLONE_BIN=C:\erp\deploy\windows\vendor\rclone.exe
```

**"has no remote called 'gdrive'"** — the rclone setup did not finish. Redo
INSTALL-WINDOWS.md step 8.2. Check with:

```
C:\erp\deploy\windows\vendor\rclone.exe listremotes
```

It must print `gdrive:`.

**"the token has expired"** — Google revoked the permission, which happens if
the password changed or it went unused for six months. Reconnect:

```
C:\erp\deploy\windows\vendor\rclone.exe config reconnect gdrive:
```

It opens a browser to sign in again.

**"took longer than 900 seconds"** — the connection is too slow for the size of
the backup. Raise the limit in `C:\erp\.env`:

```
BACKUP_RCLONE_TIMEOUT=3600
```

### 5.4 — The USB copy is "skipped"

That is a **warning, not a failure**, and the run still counts as `0x0`. The
stick is not plugged in. Plug it in, or set the right drive letter in
`C:\erp\.env`:

```
BACKUP_USB_PATH=E:\erp-backups
```

Make that folder on the stick first. Note the drive letter can change between
sticks and between USB ports.

### 5.5 — The task never runs at all

In Task Scheduler, open **ERP Nightly Backup** → **General** tab:

- **"Run whether user is logged on or not"** must be ticked. If it is not, the
  task only runs while somebody is signed in.
- Check the **Last Run Time**. If it says *"Never"*, right-click → **Run** and
  watch what happens.
- If the PC is switched off at 21:00, **StartWhenAvailable** makes it run at the
  next switch-on. Check the PC is actually being left on.

---

## 6. Undoing an update

`update.bat` saves the previous version before it changes anything, and takes a
backup of the data before that. Both are needed to go back.

### 6.1 — Find the rollback copy

```
dir C:\erp\.rollback
```

The folders are named by date and time, e.g. `20260809-143022`. **The newest one
is the version you were on before the last update.**

### 6.2 — Put the program files back

In a command window **as administrator**, replacing the timestamp with yours:

```
net stop ERP

robocopy C:\erp\.rollback\20260809-143022\apps      C:\erp\apps      /MIR
robocopy C:\erp\.rollback\20260809-143022\config    C:\erp\config    /MIR
robocopy C:\erp\.rollback\20260809-143022\templates C:\erp\templates /MIR
robocopy C:\erp\.rollback\20260809-143022\static    C:\erp\static    /MIR
copy /Y C:\erp\.rollback\20260809-143022\manage.py        C:\erp\
copy /Y C:\erp\.rollback\20260809-143022\serve.py         C:\erp\
copy /Y C:\erp\.rollback\20260809-143022\requirements.txt C:\erp\

cd C:\erp
.venv\Scripts\python.exe manage.py collectstatic --noinput --clear --settings=config.settings.prod
net start ERP
```

Then check:

```
.venv\Scripts\python.exe manage.py preflight --settings=config.settings.prod
```

### 6.3 — If the database also needs going back

Only if the update ran a migration that the old code cannot read — the symptom
is errors like *"no such column"* after 6.2.

`update.bat` took a backup immediately before the update. Find it:

```
dir C:\erp\data\backups
```

The newest file with the date and time of the update is the one. Then follow
[section 10](#10-getting-a-backup-back).

> **Anything entered since the update is lost** if you do this. If the office
> has been working all day on the new version, ring whoever supplied the update
> before restoring — going forward is usually better than going back.

---

## 7. Python is the wrong version

**Symptom:** `install.bat` says *"Python 3.12 was not found"*, or
`python --version` prints something other than 3.12.

List every Python on the machine:

```
py -0
```

- **If 3.12 is in the list**, `install.bat` will find it — it asks for 3.12
  specifically. If it still says no, the `py` launcher is missing: reinstall
  from the bundled installer and tick **"Add python.exe to PATH"**.
- **If 3.12 is not in the list**, install it from the `python` folder on the USB
  stick. Tick **"Add python.exe to PATH"**.

**Having 3.11 or 3.13 as well is fine.** They do not conflict. Do not uninstall
another Python to make room — something else on the PC may need it.

**Why 3.12 exactly?** The packages in the zip were downloaded as `cp312` builds
— compiled for that version — and pip will refuse to install them on anything
else.

---

## 8. Nobody can log in

### 8.1 — Wrong password

Somebody with an administrator login can reset anybody's: **Users** in the left
menu → click the person → **Reset password**. They will be made to choose a new
one the first time they sign in.

### 8.2 — The administrator password is lost

There is no way to recover it — it is stored one-way, on purpose. Make a new
administrator:

```
cd C:\erp
.venv\Scripts\python.exe manage.py createsuperuser --settings=config.settings.prod
```

Answer the three questions. Log in as that new user, then fix or delete the old
one from the **Users** screen.

### 8.3 — Everybody was signed out at once

The `SECRET_KEY` in `C:\erp\.env` changed. Sessions are signed with it, so all
of them became invalid at the same moment. Everyone logs in again and that is
the whole effect — no data is affected.

If nobody changed it deliberately, check `.env` was not replaced or restored
from somewhere.

### 8.4 — "Your password must be changed"

Working as intended. A login created by an administrator has a password that
administrator knows, so the first thing it must do is change it. Fill the form
in and it goes away.

---

## 9. The pages look like plain text with no colours

**Symptom:** the ERP works but looks like an unformatted document — no menu
layout, no colours, black text on white.

The CSS is not being served. Almost always `collectstatic` did not run, or ran
before the files were in place.

```
cd C:\erp
.venv\Scripts\python.exe manage.py collectstatic --noinput --clear --settings=config.settings.prod
net stop ERP
net start ERP
```

Then hold **Ctrl** and press **F5** in the browser to force it to re-fetch.

If it is still plain, run the preflight — it checks for exactly this and will
say `FAIL` on the static files line.

> Nothing here is downloaded from the internet, ever. All the styling is inside
> `C:\erp\static\dist` and was built before the zip was made. A page with no
> styling on a PC with no internet is *always* a collectstatic problem, never a
> connection problem.

---

## 10. Getting a backup back

**This replaces everything. Anything entered since the backup was taken is
gone.** A copy of the current database is saved first, so this is itself
undoable.

1. **Stop the ERP.** Restore refuses to run while it is up.

   ```
   net stop ERP
   ```

2. **If the backup is on Google Drive or a USB stick**, copy it into
   `C:\erp\data\backups` first:

   ```
   C:\erp\deploy\windows\vendor\rclone.exe copy gdrive:erp-backups/erp-20260809-2100.zip C:\erp\data\backups\
   ```

3. **Check the file before you use it.** This changes nothing:

   ```
   cd C:\erp
   .venv\Scripts\python.exe manage.py restore data\backups\erp-20260809-2100.zip --verify-only --settings=config.settings.prod
   ```

   It prints when the backup was taken and what is in it.

   > If it says the file is **damaged**, stop. Use an older backup. Restoring a
   > damaged file over a working system is the one mistake here that cannot be
   > undone.

4. **Restore it:**

   ```
   .venv\Scripts\python.exe manage.py restore data\backups\erp-20260809-2100.zip --settings=config.settings.prod
   ```

   It asks you to type `yes`. It then checks the file, saves the current
   database, restores, updates the schema, and prints the row counts before and
   after so you can see it landed.

5. **Start it again and check:**

   ```
   net start ERP
   .venv\Scripts\python.exe manage.py preflight --settings=config.settings.prod
   ```

6. Log in and check the last few invoices are the ones you expect for that date.

---

## If none of this helped

Collect these before asking for help — with them the answer is usually five
minutes, without them it is a day:

1. The output of:

   ```
   cd C:\erp
   .venv\Scripts\python.exe manage.py preflight --settings=config.settings.prod
   ```

2. The last 50 lines of `C:\erp\logs\erp.log`.
3. The last 50 lines of `C:\erp\logs\service-err.log`.
4. What you were doing when it went wrong, and the exact words of any message on
   screen — a photograph of the screen is fine.
5. Whether it affects **this PC only** or **every PC**.
