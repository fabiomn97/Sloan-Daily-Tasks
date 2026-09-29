# Canvas Daily Digest — setup (Windows)

Follow these in order. Every command is meant to be copied and pasted exactly
as written. About 20 minutes.

Commands go in **PowerShell**: press the Windows key, type `powershell`, press
Enter. A window with a text prompt opens.

---

## Step 0 — Check Python

Paste this and press Enter:

```
python --version
```

**If you see `Python 3.x.x`** — you're set, go to Step 1.

**If the Microsoft Store opens, or you see "Python was not found"** — Python
isn't installed. Get it from https://www.python.org/downloads/ and run the
installer. On the first screen, **tick "Add python.exe to PATH"** at the bottom
before clicking Install. This matters and it's easy to miss. Then close
PowerShell, open it again, and run the command above.

---

## Step 1 — Canvas access token

1. Go to **https://YOUR-SCHOOL.instructure.com/profile/settings (or canvas.yourschool.edu)**
2. Scroll to **Approved Integrations**
3. Click **+ New Access Token**
4. Purpose: `daily digest`. Set the expiry as far ahead as it allows.
5. Click **Generate Token**
6. Copy the long string. Canvas shows it once and never again.

---

## Step 2 — Gmail app password

Gmail won't accept a normal password from a script. You need a 16-character app
password, which requires 2-Step Verification first.

1. Sign into **your.address@gmail.com**
2. Turn on 2-Step Verification at **https://myaccount.google.com/security** if
   it isn't already on
3. Go to **https://myaccount.google.com/apppasswords** — type the URL directly,
   it isn't linked from any menu
4. Type a label like `canvas digest`, click **Create**
5. Copy the 16-character code Google shows you

You don't invent this code. Google generates it.

---

## Step 3 — Put the files in place

Download all five files from the chat first. They land in Downloads. Then paste
this whole block:

```
mkdir $HOME\canvas-digest
Move-Item $HOME\Downloads\canvas_digest.py,$HOME\Downloads\course_schedule.json,$HOME\Downloads\config.example.json,$HOME\Downloads\run_digest.bat,$HOME\Downloads\SETUP.md $HOME\canvas-digest\
```

Now create your real config and open it:

```
cd $HOME\canvas-digest
Copy-Item config.example.json config.json
notepad config.json
```

Notepad opens. Replace four values, keeping the quotation marks around each:

| Field | What goes here |
|---|---|
| `canvas_token` | The token from Step 1 |
| `smtp_user` | The Gmail doing the sending |
| `smtp_password` | The 16-character code from Step 2 |
| `mail_to` | Where you want the digest delivered |

Save with `Ctrl + S`, then close Notepad.

---

## Step 4 — Test

**Check that Canvas matched your syllabi:**

```
python canvas_digest.py --check
```

You should see seven courses, each with a matched course number. Anything
saying `NO SYLLABUS MATCH` needs fixing — send me the course name.

**Build a real digest without sending it:**

```
python canvas_digest.py --dry-run
start digest_preview.html
```

Your browser opens the digest built from your real Canvas data.

**Send one for real:**

```
python canvas_digest.py
```

Check your inbox.

---

## Step 5 — Schedule it for 7:00am daily

```
schtasks /create /tn "Canvas Digest" /sc daily /st 07:00 /tr "%USERPROFILE%\canvas-digest\run_digest.bat"
```

A console window flashes briefly each morning when it runs. That's normal.

**Catch missed runs.** By default Windows skips the task if the laptop was off
or asleep at 7:00. To fix that:

1. Press Windows key, type `Task Scheduler`, open it
2. Click **Task Scheduler Library** in the left panel
3. Find **Canvas Digest**, right-click, choose **Properties**
4. **Conditions** tab: tick **Wake the computer to run this task**
5. **Settings** tab: tick **Run task as soon as possible after a scheduled
   start is missed**
6. Click OK

**Confirm it's registered:**

```
schtasks /query /tn "Canvas Digest"
```

---

## Changing things later

**Different time?** Delete and recreate:

```
schtasks /delete /tn "Canvas Digest" /f
schtasks /create /tn "Canvas Digest" /sc daily /st 08:30 /tr "%USERPROFILE%\canvas-digest\run_digest.bat"
```

**Different settings?** Open `config.json` in Notepad:

- `days_ahead` — how far ahead the agenda runs (default 7)
- `announcement_lookback_days` — how far back announcements go (3)
- `hide_submitted` — `true` hides work already turned in
- `send_when_empty` — `false` skips quiet days entirely

**Turn it off:**

```
schtasks /delete /tn "Canvas Digest" /f
```

---

## If something breaks

Check the log:

```
Get-Content $HOME\canvas-digest\digest.log -Tail 20
```

| What you see | What it means |
|---|---|
| `Canvas rejected the access token (401)` | Token expired or revoked. Redo Step 1. |
| `Email login failed` | App password wrong, or 2-Step Verification was switched off. Redo Step 2. |
| `config.json isn't valid JSON` | A quote or comma got deleted while editing. Compare against `config.example.json`. |
| `No active courses found` | Canvas sees no current enrollments. |
| `NO SYLLABUS MATCH` | A Canvas course name doesn't match the syllabus file. Send me the name. |
| `Python was not found` | Python isn't on PATH. Reinstall with that box ticked. |
| Empty log | The task never ran. Recheck Step 5. |

Run it by hand any time to see errors immediately:

```
cd $HOME\canvas-digest
python canvas_digest.py
```
