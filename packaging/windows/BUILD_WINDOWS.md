# Building & Shipping Friday for Windows

The Windows build packages Friday into `FridaySetup.exe` — a normal Windows
installer your friend double-clicks. First launch opens a setup wizard that
collects their Telegram bot, Gemini key, and Google Calendar access. After
that, Friday lives in the system tray and talks to them through Telegram.

## What's different from the Mac version

| macOS                          | Windows                                  |
|--------------------------------|------------------------------------------|
| Apple Calendar (JXA)           | **Google Calendar** (API, OAuth)         |
| rumps menubar + LaunchAgent    | pystray tray app that supervises the core|
| Config/db/logs in the repo dir | `%APPDATA%\Friday\`                      |
| Voice satellite                | Not included (out of scope for v1)       |
| gcal_sync (Google → Apple)     | Not needed — Google *is* the event store |

Backend selection is automatic (`calendar.backend` defaults to `google` on
Windows, `apple` on macOS), so this one codebase serves both.

## One-time: create the Google OAuth client

The wizard's "Connect Google Calendar" button needs an OAuth client. You
create it once and bundle it with the installer:

1. Go to https://console.cloud.google.com → create a project (e.g. "Friday").
2. **APIs & Services → Library** → enable **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** → External → fill in app name
   and your email. Add the scope `https://www.googleapis.com/auth/calendar`.
4. While the consent screen is in *Testing* mode, add your friend's Gmail
   address under **Test users** (testing mode allows up to 100).
5. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   → Application type **Desktop app**.
6. Download the JSON and save it as
   `packaging/windows/google_client_secret.json` (gitignored).

Notes:
- For a Desktop-app client this file is not a real secret (Google's own
  docs say installed-app clients can't keep secrets), but keep it out of
  git anyway.
- Tokens in *Testing* mode expire after 7 days **unless** the test user's
  refresh token is kept fresh — Friday refreshes automatically on every
  poll, so in practice it stays alive. If it ever lapses, the friend
  re-runs the wizard from the tray menu ("Run Setup Wizard").

## Build option A — GitHub Actions (no Windows machine needed)

1. Push the repo to GitHub.
2. (Optional, recommended) Add a repository secret
   `GOOGLE_CLIENT_SECRET_JSON` containing the full text of the client
   secret JSON — the workflow bundles it so the wizard never asks for it.
3. Actions → **Build Windows installer** → Run workflow.
4. Download the `FridaySetup` artifact and send `FridaySetup.exe` to your
   friend.

## Build option B — on a Windows machine

1. Install Python 3.12 (x64) and Inno Setup 6 (https://jrsoftware.org/isdl.php).
2. Clone the repo, drop `google_client_secret.json` into `packaging/windows/`.
3. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
   ```
4. Send `packaging\windows\Output\FridaySetup.exe`.

## What your friend does

1. Run `FridaySetup.exe` → Next/Next/Finish (the "start with Windows" box
   is checked by default).
2. The setup wizard opens:
   - **Telegram**: it walks them through creating a bot with @BotFather,
     then auto-detects their chat ID after they message the bot once.
   - **Gemini**: link to Google AI Studio; paste the free API key.
   - **Google Calendar**: one "Connect" click → browser consent → pick a
     default calendar and which calendars show up in briefings.
   - **Canvas / weather**: optional, skippable.
3. Friday appears in the tray and says hello on Telegram.

Everything lives in `%APPDATA%\Friday` (config, SQLite db, logs, Google
token). The tray menu has Brief Me Now, Pause, Open Dashboard
(http://127.0.0.1:5174), Restart, and Run Setup Wizard.

## Troubleshooting

- **Friday never comes online**: check `%APPDATA%\Friday\logs\friday.log`
  and `tray.log`. A crash loop backs off and retries every 30 s.
- **"Access blocked" during Google consent**: the friend's Gmail isn't in
  the OAuth client's Test users list (step 4 above).
- **Times look wrong**: timezone is set in the wizard (or the dashboard);
  briefings refuse to fire outside sane local windows by design.
- **Updating Friday**: build a new FridaySetup.exe and run it over the old
  install — config and data in `%APPDATA%` are untouched.
