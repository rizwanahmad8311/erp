# Verification checklist — first install on a Windows PC

**Why this exists.** The release was built and tested on macOS. Everything that
*can* be verified without Windows has been (the list is at the bottom of this
file), but the Windows-only half — the Python installer, the `.bat` files, the
NSSM service, the firewall rule, Task Scheduler, and printing — has **not been
run on Windows**. The first person to install this is therefore also the person
testing it.

**Do this on the first install, and only the first.** It takes about 40 minutes
on top of the install itself. Later installs of the same release just follow
INSTALL-WINDOWS.md.

**How to use it.** Work top to bottom. Tick each line. When something does not
match, write down **exactly** what happened — the words on screen, and a
photograph of them — and report it. A failure here is a bug in the release, not
a mistake by you.

Ideally do this on a **clean or spare PC** first, not the one the business
depends on.

```
Installer name: ______________________  Date: ____________
Windows version (Settings > System > About): ____________________
PC name: ______________________  Release version: ________________
```

---

## A. Before you start

| # | Check | Expected | ✓ / ✗ | Notes |
|---|-------|----------|-------|-------|
| A1 | The zip is on a USB stick and opens | The file list shows `manage.py`, `deploy`, `apps` | ☐ | |
| A2 | You are signed in to Windows as an administrator | Settings → Accounts says "Administrator" | ☐ | |
| A3 | This PC has **no** Python installed yet, or has 3.12 | `py -0` in a command window | ☐ | |
| A4 | Free disk space on C: | At least 2 GB | ☐ | |

---

## B. Step 1 — Python

| # | Check | Expected | ✓ / ✗ | Notes |
|---|-------|----------|-------|-------|
| B1 | The `python` folder in the zip has an `.exe` in it | `python-3.12.10-amd64.exe` | ☐ | |
| B2 | The installer runs | The window with "Install Now" appears | ☐ | |
| B3 | **The "Add python.exe to PATH" tick-box exists where the guide says** | Bottom of the first screen | ☐ | ← *guide accuracy* |
| B4 | After installing, `python --version` works in a NEW command window | `Python 3.12.10` | ☐ | |
| B5 | `py -3.12 -c "print(1)"` prints `1` | `1` | ☐ | |

> B3 and B4 are checking the **document**, not the software. If the screen does
> not look like the guide describes, the guide needs fixing.

---

## C. Step 2 — Unzipping

| # | Check | Expected | ✓ / ✗ | Notes |
|---|-------|----------|-------|-------|
| C1 | Extract to `C:\erp` | Finishes without an error | ☐ | |
| C2 | `C:\erp\manage.py` exists | Visible in Explorer | ☐ | |
| C3 | `C:\erp\deploy\windows\install.bat` exists | Visible | ☐ | |
| C4 | The files are **not** one folder too deep | No `C:\erp\erp-release-0.1.0\` | ☐ | |
| C5 | `C:\erp\deploy\windows\wheels` has ~15 `.whl` files | 15 files | ☐ | |
| C6 | `C:\erp\deploy\windows\vendor\nssm.exe` exists | ~330 KB | ☐ | |

---

## D. Step 3 — install.bat

Run it **as administrator**. Record the result of every numbered step it prints.

| # | Check | Expected | ✓ / ✗ | Notes |
|---|-------|----------|-------|-------|
| D1 | `[1/9]` admin check | `OK - running as Administrator` | ☐ | |
| D2 | `[2/9]` Python check | `OK - found Python 3.12.x` | ☐ | |
| D3 | `[3/9]` virtual environment | `OK - created C:\erp\.venv` | ☐ | |
| D4 | `[4/9]` packages | `OK - packages installed`, **no network errors** | ☐ | ← *the offline test* |
| D5 | `[5/9]` settings file | `Wrote C:\erp\.env` and a detected IP | ☐ | |
| D6 | `[6/9]` database | `OK - database ready, web pages collected` | ☐ | |
| D7 | `[7/9]` asks for a username and password | The three prompts appear | ☐ | |
| D8 | The password is **invisible** as you type | Nothing appears, not even dots | ☐ | |
| D9 | `[8/9]` service | `OK - service "ERP" installed and started` | ☐ | |
| D10 | `[9/9]` firewall | `OK - port 8000 is open to the local network` | ☐ | |
| D11 | The preflight at the end | Every line `OK`, then `All checks passed` | ☐ | |
| D12 | The final banner prints a LAN address | `http://192.168.x.x:8000` | ☐ | |
| D13 | Total time from double-click to finished | ______ minutes | ☐ | |

**If any step failed, write the exact message here:**

```
Step: ____   Message:
_________________________________________________________________
_________________________________________________________________
```

---

## E. The service

| # | Check | Command / Where | Expected | ✓ / ✗ |
|---|-------|-----------------|----------|-------|
| E1 | The service exists | `sc query ERP` | `STATE : 4  RUNNING` | ☐ |
| E2 | It is set to start automatically | `sc qc ERP` | `START_TYPE : 2  AUTO_START` | ☐ |
| E3 | It has a readable name in the Services list | `services.msc` | "Distribution ERP" | ☐ |
| E4 | It logs where expected | `C:\erp\logs\` | `service-out.log` present | ☐ |
| E5 | The application log exists | `C:\erp\logs\erp.log` | Has an `ERP serving on…` line | ☐ |
| E6 | **It survives a reboot** | Restart the PC, wait 2 min, `sc query ERP` | `RUNNING`, nobody logged in | ☐ |
| E7 | It restarts if killed | Task Manager → end `python.exe` → wait 30s → `sc query ERP` | `RUNNING` again | ☐ |

> **E6 is the most important line on this page.** It is the whole reason the ERP
> is a service instead of a program somebody starts. Do not skip it.

---

## F. Steps 4–5 — Logging in

| # | Check | Expected | ✓ / ✗ | Notes |
|---|-------|----------|-------|-------|
| F1 | `http://localhost:8000` in a browser | The login page | ☐ | |
| F2 | **The page is styled** — colours, a menu down the left | Not plain black-on-white text | ☐ | ← *WhiteNoise* |
| F3 | Logging in with the step-3 details works | The dashboard appears | ☐ | |
| F4 | The dashboard shows cards and a chart | Not an error page | ☐ | |
| F5 | Changing the password works | "Password changed" | ☐ | |
| F6 | Logging out and back in with the **new** password works | | ☐ | |

---

## G. Steps 6–7 — The network

| # | Check | Command / Where | Expected | ✓ / ✗ |
|---|-------|-----------------|----------|-------|
| G1 | `ipconfig` shows an IPv4 address | On the server | e.g. `192.168.1.50` | ☐ |
| G2 | It matches what install.bat printed | | Same number | ☐ |
| G3 | The firewall rule exists | `wf.msc` → Inbound Rules | "Distribution ERP (port 8000)" | ☐ |
| G4 | **Another PC can open the login page** | On a different PC: `http://192.168.1.50:8000` | Login page | ☐ |
| G5 | That other PC can log in and post an invoice | | Works | ☐ |
| G6 | An unrelated device on guest Wi-Fi **cannot** reach it | Phone on guest network | Times out | ☐ |

> G4 is the second most likely thing to fail. If it does, work through
> TROUBLESHOOTING.md section 2 and **record which of 2.1–2.6 was the cause** —
> that tells us which one to promote to the top of the list.

Cause, if G4 failed: ______________________________________________

---

## H. Step 8 — rclone and Google Drive

Skip this whole section if the PC has no internet; note that here and move on.

| # | Check | Expected | ✓ / ✗ | Notes |
|---|-------|----------|-------|-------|
| H1 | `rclone.exe` is in `C:\erp\deploy\windows\vendor\` | ~85 MB | ☐ | |
| H2 | `rclone.exe config` starts | The `n/s/q>` prompt | ☐ | |
| H3 | **The prompts match the guide word for word** | See INSTALL-WINDOWS.md 8.2 | ☐ | ← *guide accuracy* |
| H4 | The browser opens for Google sign-in | | ☐ | |
| H5 | The "Google hasn't verified this app" warning appears as described | Advanced → Go to rclone | ☐ | |
| H6 | `rclone.exe listremotes` prints `gdrive:` | | ☐ | |
| H7 | `rclone.exe lsd gdrive:` lists folders | | ☐ | |

**If the prompts differ from the guide, write down what rclone actually asked:**

```
_________________________________________________________________
_________________________________________________________________
```

---

## I. Step 9 — The nightly backup task

| # | Check | Where | Expected | ✓ / ✗ |
|---|-------|-------|----------|-------|
| I1 | The XML imports without an error | Task Scheduler → Import Task | Properties window opens | ☐ |
| I2 | The three `EDIT-ME` paths are right for `C:\erp` | In the Actions tab | No editing needed | ☐ |
| I3 | The task appears in the library | "ERP Nightly Backup" | ☐ | |
| I4 | Right-click → Run finishes | Wait 30s, F5 | Last Run Result `0x0` | ☐ |
| I5 | A new zip appeared | `C:\erp\data\backups\` | `erp-<date>-<time>.zip` | ☐ |
| I6 | It reached Google Drive | Check the Drive folder in a browser | The same file | ☐ |
| I7 | The Backup screen in the ERP shows the run | Backup in the left menu | A green "Local" row | ☐ |

---

## J. Step 10 — Proving it works

| # | Check | Expected | ✓ / ✗ | Notes |
|---|-------|----------|-------|-------|
| J1 | `manage.py preflight` — every line | All `OK`, "All checks passed" | ☐ | |
| J2 | Create and post a test invoice | Status becomes **Posted** | ☐ | |
| J3 | Ctrl+P shows a print preview | The invoice, laid out | ☐ | |
| J4 | **The office printer is in the printer list** | | ☐ | |
| J5 | It prints on paper, readable, with the total | | ☐ | |
| J6 | The amount in words is right, in **lakh/crore** | Not "million" | ☐ | |
| J7 | The PDF button downloads a PDF that opens | | ☐ | |
| J8 | "Back up now" on the Backup screen works | New file appears | ☐ | |
| J9 | Cancelling the test invoice works | Status **Cancelled**, still listed | ☐ | |
| J10 | The dashboard reflects the test invoice, then the cancellation | Sales today changes, then returns | ☐ | |

---

## K. update.bat  *(only if a second release exists)*

Do this once, with any newer build, before it is ever needed in anger.

| # | Check | Expected | ✓ / ✗ | Notes |
|---|-------|----------|-------|-------|
| K1 | `update.bat C:\erp-new` stops the service | `[2/7] Stopping the ERP... OK` | ☐ | |
| K2 | It takes a backup first | `[3/7] ... OK - backup taken` | ☐ | |
| K3 | A rollback folder appears | `C:\erp\.rollback\<timestamp>\` | ☐ | |
| K4 | It migrates and collects static | `[6/7] ... OK` | ☐ | |
| K5 | It restarts and the checks pass | `UPDATED.` | ☐ | |
| K6 | **The data is still there** — the test invoice, the users | Log in and look | ☐ | |
| K7 | `.env` was **not** overwritten | Same SECRET_KEY, still logged in | ☐ | |
| K8 | Rolling back per TROUBLESHOOTING.md §6 works | Old version runs again | ☐ | |

---

## L. uninstall.bat  *(on a spare PC only — never on the live one)*

| # | Check | Expected | ✓ / ✗ | Notes |
|---|-------|----------|-------|-------|
| L1 | It asks for `yes` before doing anything | Typing anything else changes nothing | ☐ | |
| L2 | The service is gone | `sc query ERP` → does not exist | ☐ | |
| L3 | The firewall rule is gone | `wf.msc` | ☐ | |
| L4 | The nightly task is gone | Task Scheduler | ☐ | |
| L5 | **`C:\erp\data\erp.sqlite3` still exists** | Explorer | ☐ | |
| L6 | **`C:\erp\data\backups\` still has the backups** | Explorer | ☐ | |
| L7 | `C:\erp\.env` still exists | Explorer | ☐ | |
| L8 | Re-running `install.bat` brings it all back, same data | Log in, test invoice still there | ☐ | |

---

## M. Failure drills

The point of these is that they happen for the first time **while somebody who
knows the system is standing there**, not at 8am on a Monday.

| # | Drill | Do this | Expected | ✓ / ✗ |
|---|-------|---------|----------|-------|
| M1 | Power cut | Hold the power button, switch back on, wait 2 min | The ERP answers with nobody logging in | ☐ |
| M2 | Backup with the USB unplugged | Unplug, run a backup | Result `0x0`, USB "skipped" not "failed" | ☐ |
| M3 | Backup with no internet | Unplug the network, run `backup --push` | Result `0x1`, local zip still written | ☐ |
| M4 | Port taken | Start something else on 8000, restart the service | `service-err.log` names the port clash | ☐ |
| M5 | Restore | Restore the backup from I5 per TROUBLESHOOTING §10 | Row counts print, ERP works | ☐ |
| M6 | Wrong password 5 times | Try to log in with a bad password | Refused each time, no lockout of the PC | ☐ |

---

## Sign-off

```
Everything above ticked?            YES / NO
Anything that failed is written down and reported?   YES / NO

Left with the owner on paper:
  ☐ the LAN address for the other PCs
  ☐ the administrator username and password
  ☐ which Google account holds the backups
  ☐ a printed copy of TROUBLESHOOTING.md

Installer signature: ____________________   Date: ____________
```

---

## Appendix — what was already verified on macOS

So you know which lines above are a formality and which are genuinely first-run.

**Verified by building and running the release:**

- The release zip builds, is 74 MB, and contains the application, the compiled
  CSS/JS, 15 wheels, `nssm.exe`, `rclone.exe`, the Python 3.12.10 installer, the
  scheduled-task XML and both documents.
- The zip contains **no** `data/`, `media/`, `.env`, `static/src`, tests or
  `__pycache__`. The build refuses to produce a zip that does.
- **The offline install resolves.** `pip install --no-index --find-links=wheels`
  targeting `win_amd64` / `cp312` installs all 11 runtime packages with the
  network unavailable — including Pillow and charset-normalizer, which have
  native code and arrived as prebuilt `.pyd` wheels. No compiler is needed.
- Unzipping the release and running `migrate`, `collectstatic`, `backup` and
  `preflight` from nothing produces a working installation: 5 permission groups,
  36 chart-of-accounts rows, a backup zip, and every preflight check green.
- `serve.py` binds `0.0.0.0:8000` with 8 threads, logs its start line to
  `logs/erp.log`, answers HTTP, and `preflight --service` detects it.
- `preflight` correctly **fails** when `.env` is missing, when `collectstatic`
  has not run, and when this machine's own address is not in `ALLOWED_HOSTS` —
  and names the fix in each case.
- One real bug was found and fixed this way: the release ships no `data/`
  folder, so SQLite could not create the database on a fresh install and
  `migrate` died with "unable to open database file". The settings now create
  the folder.

**Not verified — this checklist is the test:**

- Everything in a `.bat` file. They have never been executed; they were written
  against the documented behaviour of `cmd.exe`, `nssm`, `netsh` and `schtasks`.
- The Python 3.12 installer's screens, and whether the tick-box is where the
  guide says.
- NSSM installing, starting, auto-starting and surviving a reboot.
- The firewall rule actually letting another PC through.
- Task Scheduler importing the XML.
- rclone's exact prompts on Windows (they are transcribed from rclone's
  documented flow, and rclone changes them between versions).
- Printing, on any printer.
