# Moving axon-reports from the Mac to the Windows PC

Four parts. The Mac keeps syncing until part C, so nothing is missed while the
PC is being set up. Part C (the switch) takes about 20 minutes in one sitting.

What does NOT need redoing: Amazon (SP-API) refresh tokens, the WooCommerce key
and the Flipkart app keys are not tied to a machine - the same `.env` values
work on the PC.

---

## A. On the Mac (before the move)

**A1. Check how far Seller history goes:**

    psql axon_amazon -c "SELECT min(report_date), max(report_date), count(*) FROM seller_sales_traffic_by_date;"

If it starts at 2026-06-30 (the Nov 2025 backfill hasn't run), carry on - the
backfill runs on the PC after the switch (C11), so the move isn't held up.

**A2. Put the latest files in `sp_api`.** Unzip the kit over the folder (it
replaces code files only; your CSVs, `.env`, `raw/` and `logs/` are not in it):

    unzip -o ~/Downloads/axon_reports_kit.zip -d ~/Documents/sp_api
    cd ~/Documents/sp_api
    psql axon_amazon -f schema.sql
    python3 -m pip install -r requirements.txt
    python3 mcp_server.py --check          # must end with "All tools ran."

**A3. Clean git before the first commit.**

    rm -f gitignore env.example             # older copies; the kit has .gitignore and .env.example
    git rm -r --cached --ignore-unmatch .env raw logs __pycache__ ad_spend.csv vendor_sales_raw.json
    git log --all --oneline -- .env

The last command **must print nothing**. If it prints anything, `.env` was
committed at some point: stop and don't push. The commit history has to be
cleaned first (ask Claude), and if that history was ever pushed anywhere, the
SP-API and WooCommerce keys must be replaced too.

    git add -A
    git status                              # no .env, raw/, logs/ or .xlsx in the list
    git commit -m "axon-reports: Mac + Windows"

**A4. Push to a private GitHub repo** owned by the company account (not a
personal one). On github.com: New repository -> name `axon-reports` ->
**Private** -> no README. Then:

    git remote add origin https://github.com/<company-account>/axon-reports.git
    git push -u origin main

If git asks for a password, use a GitHub personal access token (or push with
GitHub Desktop). Open the repo on github.com and confirm `.env` and `raw/` are
not there.

**A5. Note the Postgres version:** `psql --version` (the PC needs the same
major version or newer).

---

## B. On the Windows PC (setup; the Mac keeps running)

**B1. Install** (all with default options unless noted):
- **Git for Windows** - git-scm.com
- **Python 3.11 or newer** - python.org. On the first screen tick **"Add
  python.exe to PATH"**.
- **PostgreSQL**, same major version as the Mac or newer - the EDB installer
  from postgresql.org. Set a password for the `postgres` user (letters and
  digits only - no `@ : / #`, they break the connection string). Port 5432.
  Skip Stack Builder.
- **Claude Desktop**, signed in to the new account.

**B2. Put Postgres on the PATH:** Start -> "Edit environment variables for your
account" -> Path -> New -> `C:\Program Files\PostgreSQL\<version>\bin`.
Close and reopen PowerShell.

**B3. Turn on BitLocker / Device encryption** (Settings -> Privacy & security).
If company IT manages the PC, ask them to confirm it is on. This is one of the
security controls declared to Amazon for SP-API data.

**B4. Get the code** (PowerShell):

    cd $HOME\Documents
    git clone https://github.com/<company-account>/axon-reports.git sp_api
    cd sp_api
    python -m pip install -r requirements.txt

**B5. Create `.env`.** Copy the Mac's `.env` across by USB drive (never email,
chat or a cloud drive), or fill in `.env.example` and save it as `.env`. Then
change the database lines for Windows:

    DATABASE_URL=postgresql://postgres:<postgres password>@localhost:5432/axon_amazon
    MCP_DATABASE_URL=postgresql://mcp_reader:<reader password>@localhost:5432/axon_amazon

(You choose the reader password in C4.)

**B6. Create the empty database:**

    createdb -U postgres axon_amazon

---

## C. The switch (one sitting, ~20 min)

**C1. Mac - stop the schedules:**

    launchctl bootout gui/$(id -u)/com.axon.sync
    launchctl bootout gui/$(id -u)/com.axon.woo
    rm ~/Library/LaunchAgents/com.axon.sync.plist ~/Library/LaunchAgents/com.axon.woo.plist

**C2. Mac - dump the database and note the row counts:**

    cd ~/Documents/sp_api
    pg_dump -Fc -f axon_amazon.dump axon_amazon
    psql axon_amazon -f row_counts.sql

Copy `axon_amazon.dump` to the PC's `sp_api` folder by **USB drive**. It holds
Amazon data: not by email, WhatsApp or a cloud drive.

**C3. PC - restore:**

    pg_restore -U postgres -d axon_amazon --no-owner --no-privileges axon_amazon.dump
    psql -U postgres -d axon_amazon -f schema.sql
    psql -U postgres -d axon_amazon -f row_counts.sql

The row counts must match the Mac's. (Warnings about roles or ownership during
`pg_restore` are expected - `--no-owner` skips them.)

**C4. PC - the read-only login for Claude Desktop:**

    psql -U postgres -d axon_amazon -f readonly_user.sql
    psql -U postgres -d axon_amazon

At the `axon_amazon=#` prompt type `\password mcp_reader`, enter the reader
password twice, then `\q`. Put the same password in `MCP_DATABASE_URL` in `.env`.

**C5. PC - check everything:**

    python mcp_server.py --check            # must end with "All tools ran."
    python sync.py --only woo               # WooCommerce + Amazon Buy Box from the PC

**C6. PC - schedules and the Desktop connection:**

    powershell -ExecutionPolicy Bypass -File .\windows\install_schedule.ps1

It installs "Axon Sync" (10:00) and "Axon Woo" (13:00, 16:00, 19:00) and prints
a block starting with `"mcpServers"`. In Claude Desktop: Settings -> Developer
-> Edit Config, paste that block in (merge it if the file already has
`mcpServers`), save, and fully quit and reopen Claude Desktop.

**C7. Test in Claude Desktop:** "Use axon-reports: how fresh is the data?" and
"Show the Amazon MIS for September."

**C8. Skills.** The five skills are in `skills\`. For each folder, right-click
-> Compress to ZIP, and upload it in Claude's skill settings - or open a chat,
paste the folder's `SKILL.md` and ask Claude to save it as a skill.

**C9. Project context.** In the new Claude account, create a Project and add
`CLAUDE.md` to it, so chats there know the rules and decisions behind the code.

**C10. Delete the dump** from the USB drive and from the PC.

**C11. Seller history back to Nov 2025** (if A1 showed it starting 30 Jun 2026).
Start it after the 10:00 run has finished, or in the evening; it takes about 7
hours and the PC must stay on:

    python sync.py --start 2025-11-01 --end 2026-06-29 --only seller

If it stops partway, find the last day it loaded and restart from the day after:

    psql -U postgres -d axon_amazon -c "SELECT max(report_date) FROM seller_sales_traffic_by_date WHERE report_date < '2026-06-30';"
    python sync.py --start <that date + 1> --end 2026-06-29 --only seller

Any days the Mac missed while switching are refilled automatically: every 10:00
run re-pulls the last 10 days.

---

## D. After a week of clean runs on the PC

- First few days: check `logs\sync.log` and ask Desktop for data freshness.
- Then clean the Mac: `dropdb axon_amazon`, delete `.env`, `raw/`, `logs/` and
  any dump, and remove `axon-reports` from the Mac's Claude Desktop config.
  Amazon data should live on one machine only.
- From now on code changes happen on the PC only, then `git commit` + `git push`.

---

## If something goes wrong

| Message | Fix |
|---|---|
| `'psql' is not recognized` | B2 (PATH), then open a new PowerShell. |
| `password authentication failed for user "mcp_reader"` | Password in `MCP_DATABASE_URL` doesn't match C4 - redo `\password mcp_reader`. |
| `password authentication failed for user "postgres"` | Fix `DATABASE_URL` in `.env`. |
| Python opens the Microsoft Store / "WindowsApps" | Reinstall Python from python.org with "Add to PATH"; turn off the python App execution aliases (Settings -> Apps -> Advanced app settings). |
| `Register-ScheduledTask : Access is denied` | Run PowerShell as administrator once for C6. |
| `UnicodeEncodeError` in a log | Sign out and back in (the installer set `PYTHONUTF8=1` for your account). |
| Desktop shows no axon-reports tools | Check the paths in the config block, then fully quit and reopen Desktop. |
