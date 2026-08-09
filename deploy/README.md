# Backups — how they work and how to get one back

> Installing on the Windows PC in the first place is `windows/INSTALL-WINDOWS.md`.
> When something is wrong, `windows/TROUBLESHOOTING.md`. This file is about the
> backups specifically.

Everything the business has is in two places: the database file and the uploaded
logo. A backup is one zip holding both, plus a `manifest.json` recording when it
was taken, which build took it, the row counts and a checksum.

There is no internet on the office PC by design, so none of this depends on one
except the Google Drive copy — and when that fails, the backup is still taken.

---

## Every night at 21:00

Task Scheduler runs `manage.py backup --push`, which:

1. Snapshots the database with SQLite's `VACUUM INTO`. **Not a file copy** — the
   database is three files while the ERP is running and copying it gets you one
   that opens fine and is missing the last few bills.
2. Zips it with the media folder and a manifest.
3. Copies it to the USB drive, if one is plugged in.
4. Uploads it to Google Drive with rclone.
5. Deletes old backups beyond the retention policy: 14 daily, 8 weekly,
   12 monthly.

To set the schedule up, see `windows/erp-backup-nightly.xml` — the instructions
are in the comment at the top of the file, and step 9 of
`windows/INSTALL-WINDOWS.md` walks through it.

**Last Run Result in Task Scheduler:**

| Code | Means |
| ---- | ----- |
| `0x0` | Worked. An unplugged USB drive counts as working. |
| `0x1` | The backup was taken, but a copy failed — usually Google Drive. |
| `0x2` | **No backup was taken.** Look at this one today. |

---

## One-time setup

### The USB drive

Put this in `.env` next to `manage.py`:

```
BACKUP_USB_PATH=E:\erp-backups
```

Then make the folder on the stick. If the stick is not plugged in the backup
still runs and still uploads — the USB copy is logged as skipped, not failed.

### Google Drive

rclone, not the Google API: there is no login code in this project to maintain
and no client secret in the repository. You do this once, on a machine with
internet.

1. Download rclone for Windows from <https://rclone.org/downloads/>.
2. Unzip it and put `rclone.exe` somewhere on the PATH, or set
   `BACKUP_RCLONE_BIN` in `.env` to its full path.
3. Run `rclone config` and create a **Google Drive** remote named `gdrive`. It
   opens a browser and asks you to sign in.
4. Check it: `rclone lsd gdrive:`

Optional, in `.env`:

```
BACKUP_RCLONE_REMOTE=gdrive:erp-backups
```

If rclone is missing or the remote is wrong, the backup screen and the command
both print exactly what to do. Neither prints a traceback.

---

## Getting a backup back

**This replaces everything.** Anything entered since the backup was taken is
gone. A copy of the current database is saved first, so a mistake here can
itself be undone.

1. **Stop the ERP.** Find the black window titled `ERP serving on ...` and press
   Ctrl+C, or close it. If it runs as a scheduled task, end it in Task
   Scheduler. `restore` refuses to run while it is up and will tell you so.

2. Open a command prompt in the ERP folder and activate the environment:

   ```
   .venv\Scripts\activate
   ```

3. Check the file first — this changes nothing:

   ```
   python manage.py restore data\backups\erp-20260809-2100.zip --verify-only
   ```

   It prints when the backup was taken and what is in it. If it says the file is
   damaged, **stop and use an older one.**

4. Restore it:

   ```
   python manage.py restore data\backups\erp-20260809-2100.zip
   ```

   It asks you to type `yes`. It then verifies the checksum, saves the current
   database, restores, runs migrations, and prints the row counts before and
   after so you can see it landed.

5. Start the ERP again:

   ```
   python serve.py
   ```

### Restoring from Google Drive

```
rclone copy gdrive:erp-backups/erp-20260809-2100.zip data\backups\
```

Then step 3 above. A file pulled off Drive or off the USB stick restores exactly
the same way — the checksum in the manifest is what proves it arrived intact.

---

## Checking it is working

Open **Backup** in the sidebar (Admin only). It shows when the last successful
backup was, and turns that line rust if it is more than 48 hours old. The
history below lists every attempt, per destination, with the reason for anything
that failed.
