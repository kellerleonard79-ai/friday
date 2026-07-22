# Task: Fix the Friday setup wizard — back-navigation + live token validation

## Context

`setup_wizard.py` is a Windows-first Tkinter first-run wizard that collects config
(Telegram bot token, chat ID, provider/model, API keys, etc.) and writes it out for
Friday. It's invoked as:

```
python3 -c "import sys; sys.frozen=True; import setup_wizard; setup_wizard.run(first_run=True)"
```

from the inner `friday/friday/` directory.

**The problem we're fixing.** The wizard has no input validation. A user entered a
Telegram bot token, was allowed to proceed, and hit an `InvalidToken` / token-rejected
error several steps later — not at the point of entry. There is no way to navigate back
to correct the token. The user then regenerated the token via BotFather as a
troubleshooting step, which *invalidated* the original token the wizard had captured,
leaving them fully wedged: the stored token is dead and the UI won't let them replace it.

**Two root causes:** (1) validation is deferred instead of happening at entry, and
(2) there's no back-navigation with value preservation.

## Before you write anything

1. Read `setup_wizard.py` in full. Determine how steps/screens are structured
   (a list of frames? a step index? a wizard/notebook widget?), how "Next" advances,
   how entered values are stored, and where/when the final config is written to disk.
2. Note the exact field keys the wizard collects and how it currently persists them.
3. Identify where `data_dir()` resolves on Windows (likely `%APPDATA%`) and confirm
   whether a partially-written config from a prior run is read back on relaunch — this
   matters for the reset affordance below.
4. Report back a 3–4 line summary of the current step model before implementing, so the
   two commits map cleanly onto the real structure. If the structure makes the commit
   split below awkward, say so and propose an alternative rather than forcing it.

Do **not** change validation behavior and navigation in the same commit. Keep them
separate and single-purpose so either can be reverted independently.

---

## Commit 1 — Back-navigation with value preservation

Goal: every step is revisitable, and nothing entered is lost when moving back and forth.

- Add a **Back button** to every step except the first. It returns to the previous step
  with all previously-entered values still populated in their fields.
- Persist entered values in an in-memory model (e.g. a single `self._values` dict keyed
  by field name) that is the single source of truth for what's rendered into each field.
  Navigating away from a step writes the field's current contents into that dict;
  navigating into a step reads from it.
- **Do not finalize/write the config file on every step.** The config should only be
  written at the final confirmation step. If the current code writes incrementally,
  refactor so it holds state in memory and writes once at the end. (This directly
  prevents the "dead token persisted to disk" wedge.)
- If a value changed since it was last validated, mark it so Commit 2's re-validation
  knows to re-check it on the way forward (a simple per-field `_validated` flag or a
  dict of last-validated values is fine).
- Keep the visual style consistent with the existing wizard. Don't restyle.

Commit message: `wizard: add back-navigation with value preservation`

---

## Commit 2 — Live validation, token masking, and recovery

Goal: move the token error from "several steps later" to the moment of entry, and make
a stuck user able to recover without touching the filesystem.

### Token field validation (on advancing from the token step)

- **Trim whitespace** (including trailing newlines) from the token before anything else —
  non-technical users paste with stray whitespace.
- **Cheap format pre-check** before any network call: token should match roughly
  `^\d+:[A-Za-z0-9_-]{30,}$`. On failure, show an inline "that doesn't look like a bot
  token" message instantly and block Next. Don't hit the network for obvious typos.
- **Live `getMe` validation**: call `https://api.telegram.org/bot<TOKEN>/getMe`. Require
  HTTP 200 and `ok: true` in the JSON. Block "Next" on failure.
- **Distinguish network failure from auth failure.** A connection/timeout error must show
  something like "Couldn't reach Telegram — check your internet connection and try again,"
  NOT "invalid token." Only an actual `ok: false` / 401 response should say the token was
  rejected. Conflating these is what makes users regenerate a perfectly good token.
- **Non-blocking with a timeout (~10s).** Run the `getMe` call off the Tk main thread
  (thread + `root.after`-style marshaling back to the UI, or equivalent) so the window
  never freezes. Disable the Next button and show a "Checking…" state while in flight,
  re-enable on result. A frozen window reads as a crash to a non-technical user.

### Chat-ID step

- The chat-ID step must validate using the **current** token value (not a stale capture).
- Distinguish "you haven't messaged the bot yet" (a successful `getUpdates` call returning
  an empty result) from an auth error. Empty updates should prompt "Send your bot a message
  in Telegram first, then retry," not an error. This is a likely spot where the original
  "few steps later" failure actually fired.

### Token masking

- In **every** error dialog, status label, and log line, mask the token — show only the
  last 4 characters (e.g. `••••••1234`). The raw token must never appear in a dialog or in
  logs. Audit the whole wizard for any existing place a token could be echoed.

### Recovery affordance

- Add a **"Start over / Reset setup"** control that clears the in-memory model and returns
  to step 1. If a partially-written or stale config file exists from a prior run (see the
  `data_dir()` note), reset should also clear/ignore it so a wedged user gets a clean
  first-run without deleting files by hand. Confirm the actual persistence location before
  wiring this — don't delete anything outside Friday's own config path.

Commit message: `wizard: live getMe/chat-id validation, token masking, reset affordance`

---

## Verification (required before you call this done)

Tkinter runs on macOS, so exercise the wizard live — compilation is not sufficient.

1. **Back-nav preserves values:** enter values, advance, go Back, confirm fields are still
   populated; change one, go forward, confirm the change persisted.
2. **Bad token blocked at entry:** enter a malformed token → instant format error, Next
   blocked, no network call. Enter a well-formed but invalid token → `getMe` runs, auth
   error shown, Next blocked, token masked in the message.
3. **Network vs auth:** simulate an unreachable network (e.g. temporarily point the base
   URL at a bad host) and confirm the message says "couldn't reach," not "invalid token."
4. **Valid token passes:** a real valid token advances cleanly and shows a "Checking…"
   state that resolves without freezing the window.
5. **Chat-ID empty-vs-auth:** confirm empty `getUpdates` prompts "message your bot first"
   rather than erroring.
6. **Reset:** trigger Start Over mid-wizard, confirm it returns to step 1 with a clean
   model and no dead token lingering.
7. **No token in logs:** grep the run's log output for the test token — it must not appear.

Report the results of each check. Keep both commits small and single-purpose.