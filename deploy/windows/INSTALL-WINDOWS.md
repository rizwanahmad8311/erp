# Installing the ERP on the office PC

Follow these steps in order. Do not skip one because it looks like it does not
apply — every step is here because leaving it out breaks something later.

**Before you start you need:**

- the PC that will hold the data, switched on, with Windows 10 or 11
- the file `erp-release-<version>.zip` (on a USB stick)
- to be signed in to Windows as an **administrator**
- about 20 minutes

**You do not need the internet.** Everything is in the zip. The only step that
uses the internet at all is step 8 (Google Drive backups), and if this PC has no
connection you can do that step later, from a different machine, or skip it —
the ERP and its local backups work without it.

**Words used below**

| Word | What it means here |
| ---- | ------------------ |
| *this PC* / *the server* | the one computer that holds the data and runs the ERP |
| *the other PCs* | every other computer in the office, which will open the ERP in a browser |
| *command window* | the black window with white text |
| *right-click → Run as administrator* | right-click the file, then click that line in the menu that appears |

---

## Step 1 — Install Python 3.12

1. Plug in the USB stick and open it in Windows Explorer.
2. Open the folder **`python`**.
3. Double-click the file inside it — it is named something like
   **`python-3.12.10-amd64.exe`**.
4. A window appears with two buttons and two tick-boxes at the bottom.

   > ### ⚠ TICK THE BOX "Add python.exe to PATH"
   >
   > It is at the **bottom** of the first screen and it is **not** ticked by
   > default. This is the single most-missed step in this whole document. If you
   > miss it, step 3 fails and you have to run the installer again.

5. Tick **"Add python.exe to PATH"**.
6. Click **"Install Now"**.
7. Wait for it to finish, then click **Close**.

*Screenshot here: the Python installer's first screen, with "Add python.exe to
PATH" circled.*

### Checking it worked

1. Press the **Windows key**, type `cmd`, press **Enter**. A command window opens.
2. Type this exactly and press Enter:

   ```
   python --version
   ```

3. It should print `Python 3.12.10` (the last number may differ; the **3.12**
   must not).

   - If it says *"'python' is not recognized"*, the tick-box in step 1.5 was
     missed. Run the installer again, choose **Modify**, and make sure
     **"Add Python to environment variables"** is ticked.
   - If it prints **3.11**, **3.13** or anything that is not 3.12, see
     TROUBLESHOOTING.md, *"Python is the wrong version"*.

4. Leave the command window open; you will use it again.

---

## Step 2 — Unzip the application to C:\erp

1. On the USB stick, right-click **`erp-release-<version>.zip`**.
2. Choose **"Extract All…"**.
3. In the box that appears, delete what is there and type exactly:

   ```
   C:\erp
   ```

4. Untick "Show extracted files when complete", then click **Extract**. It takes
   a minute or two.
5. When it finishes, open **`C:\erp`** in Windows Explorer and check you can see
   a file called **`manage.py`** and a folder called **`deploy`**.

   If instead you see a single folder called `erp-release-…`, the files went one
   level too deep. Open that folder, select everything inside it (Ctrl+A), cut
   it (Ctrl+X), go back up to `C:\erp`, and paste (Ctrl+V).

> **Why C:\erp and not somewhere else?** Every instruction in this document and
> in TROUBLESHOOTING.md says `C:\erp`. Putting it elsewhere works, but then
> every path in every step and in the nightly backup task has to be changed to
> match, and one that gets missed fails at 9pm on a Saturday.

*Screenshot here: `C:\erp` in Explorer, showing manage.py and the deploy folder.*

---

## Step 3 — Run install.bat

1. In Windows Explorer, go to **`C:\erp\deploy\windows`**.
2. **Right-click `install.bat`** and choose **"Run as administrator"**.
3. If Windows asks *"Do you want to allow this app to make changes?"*, click
   **Yes**.

A black window opens and works through nine numbered steps. It takes about five
minutes. **Do not close it.**

*Screenshot here: install.bat running, showing `[4/9] Installing the
application's packages...`*

### It will stop and ask you one question

At step 7 it asks for the **first administrator login**:

```
Username (leave blank to use 'admin'):
Email address:
Password:
Password (again):
```

- **Username** — press Enter to accept `admin`, or type another name.
- **Email address** — press Enter to leave it blank. Nothing sends email.
- **Password** — type one and press Enter.

  > **The password does not appear as you type it.** Not even dots. That is
  > normal and it is not broken. Type it, press Enter, type it again, press
  > Enter.

  If it complains the password is too short or too common, it will ask again.
  Choose a longer one.

**Write the username and password on paper now.** Nobody can recover them for
you — if they are lost the only fix is a developer running a command on this
machine.

### When it finishes

The last thing it prints looks like this:

```
===========================================================================
  INSTALLED.
===========================================================================

  On THIS PC, open:        http://localhost:8000
  On the OTHER PCs, open:  http://192.168.1.50:8000

  >>> Write this address down: http://192.168.1.50:8000
```

**Write that address down.** It is what every other computer in the office will
type. Yours will not be `192.168.1.50` — it will be whatever the installer
printed.

If it printed **INSTALLATION DID NOT FINISH** instead, read the message above it
and see TROUBLESHOOTING.md. Nothing is damaged, and `install.bat` is safe to run
again once the problem is fixed.

Press any key to close the window.

---

## Step 4 — Open the ERP and log in

1. Open a browser on this PC (Edge or Chrome).
2. In the address bar type:

   ```
   http://localhost:8000
   ```

   and press Enter.

3. A login page appears. Enter the username and password from step 3.

*Screenshot here: the login page.*

If you get *"This site can't be reached"*, see TROUBLESHOOTING.md, *"The service
will not start"*.

---

## Step 5 — Change the administrator password

The password you typed during the install was typed in a black window that other
people may have been able to see. Change it now.

1. While logged in, look at the bottom of the menu on the left.
2. Click **Password**.
3. Fill in the old password once and the new password twice.
4. Click **Change my password**.

*Screenshot here: the change-password form.*

Write the new password down and put it somewhere only the owner can get to.

---

## Step 6 — Find this PC's address on the network

The installer already printed this in step 3, and this is how to find it again
at any time.

1. Press the **Windows key**, type `cmd`, press **Enter**.
2. Type this and press Enter:

   ```
   ipconfig
   ```

3. Look for the block headed **"Wireless LAN adapter Wi-Fi"** or **"Ethernet
   adapter Ethernet"** — whichever this PC actually uses — and find the line:

   ```
   IPv4 Address. . . . . . . . . . . : 192.168.1.50
   ```

4. That number is this PC's address. **Write it down.**

*Screenshot here: an ipconfig window with the IPv4 Address line highlighted.*

**The address for every other PC in the office is:**

```
http://192.168.1.50:8000
```

replacing `192.168.1.50` with the number you just wrote down. Type it into the
browser on one of the other PCs and bookmark it.

> **If the address changes later.** The installer allowed the whole office
> network, not just this one address, so the ERP keeps working if the router
> hands this PC a different number. But the *other PCs' bookmarks* would then
> point at nothing. Ask whoever looks after the router to give this PC a fixed
> address ("DHCP reservation"); it takes them two minutes and saves this ever
> coming up.

---

## Step 7 — Open the Windows Firewall port

**`install.bat` already did this.** This step is how to check it, and how to do
it by hand if the other PCs cannot connect.

### To check

1. Press the **Windows key**, type `wf.msc`, press **Enter**.
2. Click **Inbound Rules** on the left.
3. Click the **Name** column heading to sort alphabetically, and look for:

   ```
   Distribution ERP (port 8000)
   ```

4. It should have a green tick. If it is there, this step is done.

*Screenshot here: Windows Firewall with Advanced Security, the rule highlighted.*

### To add it by hand

If the rule is missing:

1. Press the **Windows key**, type `cmd`.
2. Right-click **Command Prompt** in the results, choose **Run as administrator**.
3. Copy this line, paste it into the window (right-click pastes), press Enter:

   ```
   netsh advfirewall firewall add rule name="Distribution ERP (port 8000)" dir=in action=allow protocol=TCP localport=8000 remoteip=LocalSubnet profile=private,domain
   ```

4. It should print `Ok.`

> This allows connections **only from the office network**, and only on port
> 8000. It does not open this PC to the internet.

---

## Step 8 — Set up the Google Drive backup

This puts a copy of the accounts somewhere that survives the building burning
down. **It needs the internet**, so if this PC has none, do it later or from
another machine — everything else works without it.

If you are skipping it for now, go to step 9. The nightly backup will still run
and still write to the hard disk; only the Drive copy is skipped, and the Backup
screen will say so.

### 8.1 — Put rclone on the PC

`rclone.exe` is in **`C:\erp\deploy\windows\vendor\rclone.exe`** if the release
was built with it. Check whether it is there.

If it is not, download it on any machine with internet from
<https://rclone.org/downloads/> — choose **Windows / AMD64 - 64 Bit** — unzip
it, and copy `rclone.exe` into `C:\erp\deploy\windows\vendor\`.

Then tell the ERP where it is. Open **`C:\erp\.env`** in Notepad and add this
line at the end:

```
BACKUP_RCLONE_BIN=C:\erp\deploy\windows\vendor\rclone.exe
```

Save and close.

### 8.2 — Connect it to Google Drive

1. Open a command window (Windows key, type `cmd`, Enter).
2. Type this and press Enter:

   ```
   C:\erp\deploy\windows\vendor\rclone.exe config
   ```

3. You will now be asked a series of questions. **Here is exactly what to type
   at each one.** Anything not listed below, press Enter to accept the default.

   ```
   No remotes found, make a new one?
   n) New remote
   s) Set configuration password
   q) Quit config
   n/s/q>
   ```
   → type **`n`** and press Enter

   ```
   Enter name for new remote.
   name>
   ```
   → type **`gdrive`** and press Enter
   (this name matters — the ERP looks for a remote called exactly `gdrive`)

   ```
   Option Storage.
   Type of storage to configure.
   Choose a number from below, or type in your own value.
   ...
   18 / Google Drive
      \ (drive)
   ...
   Storage>
   ```
   → type **`drive`** and press Enter
   (type the word, not the number — the numbers change between rclone versions)

   ```
   Option client_id.
   Google Application Client Id
   ...
   client_id>
   ```
   → press **Enter** (leave it blank)

   ```
   Option client_secret.
   ...
   client_secret>
   ```
   → press **Enter** (leave it blank)

   ```
   Option scope.
   Comma separated list of scopes that rclone should use when requesting access
   1 / Full access all files, excluding Application Data Folder.
      \ (drive)
   ...
   scope>
   ```
   → type **`1`** and press Enter

   ```
   Option service_account_file.
   ...
   service_account_file>
   ```
   → press **Enter** (leave it blank)

   ```
   Edit advanced config?
   y) Yes
   n) No (default)
   y/n>
   ```
   → type **`n`** and press Enter

   ```
   Use web browser to automatically authenticate rclone with remote?
   * Say Y if the machine running rclone has a web browser you can use
   * Say N if running rclone on a (remote) machine without web browser access
   y) Yes (default)
   n) No
   y/n>
   ```
   → type **`y`** and press Enter

4. **A browser window opens** asking you to sign in to Google.
   - Sign in with the Google account the backups should go to. Use the
     **business's** account, not a personal one.
   - It warns *"Google hasn't verified this app"*. Click **Advanced**, then
     **Go to rclone (unsafe)**. This is expected — "rclone" is the program you
     just ran, and it is asking for permission to write to your own Drive.
   - Click **Continue** to grant access.
   - The browser says **"Success!"**. Close it and go back to the command window.

5. Back in the command window:

   ```
   Configure this as a Shared Drive (Team Drive)?
   y) Yes
   n) No (default)
   y/n>
   ```
   → type **`n`** and press Enter

   ```
   Configuration complete.
   Options:
   - type: drive
   - scope: drive
   - token: {"access_token":"ya29...
   Keep this "gdrive" remote?
   y) Yes this is OK (default)
   e) Edit this remote
   d) Delete this remote
   y/e/d>
   ```
   → type **`y`** and press Enter

   ```
   Current remotes:
   Name                 Type
   ====                 ====
   gdrive               drive

   e) Edit existing remote
   n) New remote
   ...
   q) Quit config
   e/n/d/r/c/s/q>
   ```
   → type **`q`** and press Enter

*Screenshot here: the rclone config screen listing `gdrive  drive`.*

### 8.3 — Check it works

In the same command window:

```
C:\erp\deploy\windows\vendor\rclone.exe lsd gdrive:
```

It should list the folders in that Google Drive account. If it prints an error,
see TROUBLESHOOTING.md, *"The backup fails"*.

---

## Step 9 — Set the nightly backup running

1. Press the **Windows key**, type `Task Scheduler`, press **Enter**.
2. In the menu at the top click **Action** → **Import Task…**.
3. Navigate to **`C:\erp\deploy\windows`** and choose
   **`erp-backup-nightly.xml`**. Click **Open**.
4. A properties window opens. On the **General** tab:
   - tick **"Run whether user is logged on or not"**
   - tick **"Run with highest privileges"**
5. Click **OK**. It asks for the Windows password of the account you are signed
   in as. Type it and click **OK**.

*Screenshot here: the Task Scheduler General tab with both boxes ticked.*

### Check it now rather than tomorrow

1. In Task Scheduler, click **Task Scheduler Library** on the left.
2. Find **"ERP Nightly Backup"** in the list.
3. Right-click it → **Run**.
4. Wait about thirty seconds, then press **F5** to refresh.
5. Look at the **Last Run Result** column:

   | It says | What it means |
   | ------- | ------------- |
   | `0x0` | Worked. Done. |
   | `0x1` | The backup was taken, but a copy failed — usually Google Drive. Check step 8. |
   | `0x2` | **No backup was taken.** See TROUBLESHOOTING.md, *"The backup fails"*. |
   | `(0x41303)` | It has not run yet. Wait and refresh again. |

> **Why 21:00?** The counter is shut, the day's postings are in, and the PC is
> still switched on. A backup at 2am is a backup that never runs, because the PC
> is off.

---

## Step 10 — Prove it works

Do all five. This is the difference between "the install finished" and "the
business can use it tomorrow morning".

### 10.1 — The automatic checks

1. Press the **Windows key**, type `cmd`, press **Enter**.
2. Copy and paste these two lines, one at a time, pressing Enter after each:

   ```
   cd C:\erp
   .venv\Scripts\python.exe manage.py preflight --settings=config.settings.prod
   ```

3. Every line should start with **`OK`** and the last line should say
   **"All checks passed"**.

   Any line starting with **`FAIL`** tells you what is wrong and what to do
   about it. Fix it and run the command again.

*Screenshot here: a full green preflight run.*

### 10.2 — Another PC can reach it

1. Go to a **different** computer in the office.
2. Open a browser and type the address from step 6, e.g.
   `http://192.168.1.50:8000`.
3. The login page should appear.

If it does not, see TROUBLESHOOTING.md, *"Other PCs cannot connect"*. **Do not
skip this test** — it is the one that fails most often, and finding out on
Monday morning is worse.

### 10.3 — A test invoice

1. On this PC, in the ERP, click **Invoices** in the left-hand menu.
2. Click **New invoice**.
3. Pick any customer, add any item and quantity, and save it.
4. Click **Post**.
5. Check the status changes to **Posted**.

### 10.4 — It prints

1. With the invoice you just posted open, press **Ctrl+P**.
2. The Windows print dialog appears with a preview of the invoice.
3. Check the office printer is in the printer list.
4. Print it. Check the paper has the company name, the lines and the total.

If the printer is not in the list, see TROUBLESHOOTING.md, *"The printer does
not appear"*.

### 10.5 — A backup runs

1. In the ERP, click **Backup** in the left-hand menu.
2. Click **Back up now**.
3. Wait. It should report success, and the new backup appears in the list below.
4. Open `C:\erp\data\backups` in Explorer and check there is a file named like
   `erp-20260809-1430.zip`.

*Screenshot here: the Backup screen after a successful run.*

### 10.6 — Undo the test

The test invoice from 10.3 is a real posted document in the accounts. Do not
delete it — it cannot be deleted and should not be. **Cancel** it:

1. Open the invoice.
2. Click **Cancel**.
3. Type a reason: `Test invoice from installation, cancelled.`
4. Confirm.

It stays in the list with a **Cancelled** mark against it, and its entries are
reversed. That is correct and it is what an audit trail looks like.

---

## Done

Leave with the owner, on paper:

- [ ] the address the other PCs use — `http://…:8000`
- [ ] the administrator username and the new password from step 5
- [ ] which Google account the backups go to
- [ ] a copy of **TROUBLESHOOTING.md** (print it)

**Tell them these three things:**

1. **This PC must stay switched on** during working hours. It is the one holding
   the data; when it is off, nobody can enter anything.
2. **The backup runs at 9pm.** The PC needs to be on. If it was off, the backup
   runs as soon as it is switched on next.
3. **Check the Backup screen once a week.** It goes rust-coloured if the last
   backup is more than two days old. That is the one thing worth looking at.
