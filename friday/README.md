# Project Friday — Setup Guide

## Prerequisites

- macOS (M1 or later)
- Python 3 installed
- VS Code (for editing files)
- Terminal

---

## Step 1 — Open Terminal and navigate to the friday folder

```bash
cd ~/friday
```

*(Move the friday folder to your home directory first if needed.)*

---

## Step 2 — Install dependencies

```bash
pip3 install -r requirements.txt
```

---

## Step 3 — Set your Anthropic API key

Run this in Terminal (replace with your actual key):

```bash
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
```

To make this permanent so you don't have to re-run it every time:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-your-key-here"' >> ~/.zshrc
source ~/.zshrc
```

---

## Step 4 — Edit friday_config.yaml

Open `friday_config.yaml` in VS Code and fill in:

1. `your_imessage_handle` — your phone number or Apple ID email used for iMessage
   Example: `+12055551234` or `you@icloud.com`

2. `groupme > api_token` — your GroupMe API token
   Get it from: https://dev.groupme.com → scroll down to "Access Token"

3. `agent > timezone` — your timezone
   Example: `America/Chicago`, `America/New_York`, `America/Los_Angeles`

4. `agent > briefing_time` — what time you want your evening briefing (24h format)
   Example: `"21:00"` for 9 PM

---

## Step 5 — Grant Full Disk Access to Terminal

Friday needs to read your Messages database to send iMessages.

1. Open **System Settings**
2. Go to **Privacy & Security → Full Disk Access**
3. Click the **+** button
4. Add **Terminal** (found in /Applications/Utilities/)
5. Make sure the toggle is ON

---

## Step 6 — Run Friday

```bash
python3 friday.py
```

You should see startup logs in the terminal and receive an iMessage from yourself confirming Friday is online.

---

## Stopping Friday

Press `Ctrl+C` in the terminal window where Friday is running.

---

## Running Friday in the background (optional)

To keep Friday running after you close the terminal window:

```bash
nohup python3 friday.py > logs/friday.log 2>&1 &
echo $! > logs/friday.pid
```

To stop it later:

```bash
kill $(cat logs/friday.pid)
```

---

## Checking logs

```bash
tail -f logs/friday.log
```

This shows live log output — useful for seeing what Friday is doing.

---

## Troubleshooting

**"ANTHROPIC_API_KEY not set"**
→ Run `export ANTHROPIC_API_KEY="your-key"` in the same terminal before running Friday.

**"GroupMe api_token is empty"**
→ Open friday_config.yaml and paste your token into the `api_token` field.

**iMessage not sending**
→ Make sure Terminal has Full Disk Access (Step 5).
→ Make sure `your_imessage_handle` matches exactly how your number appears in iMessage.

**"No approved groups found"**
→ The group name in friday_config.yaml must match exactly (case-insensitive) what appears in GroupMe.
→ Run Friday and check logs/friday.log for what group names were found.

**Import errors**
→ Run `pip3 install -r requirements.txt` again from inside the friday folder.
