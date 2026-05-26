"""
friday.py
Project Friday — Main entry point. Phase 2.
Adds: two-way iMessage, Apple Calendar, permission gate.

Usage:
    python3 friday.py

Requirements:
    pip3 install google-genai requests pyyaml schedule
    export GEMINI_API_KEY="your-key-here"
"""

import json
import logging
import os
import sys
import yaml
import schedule
import time
import threading
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "state.json")

# ── Logging setup ─────────────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/friday.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("friday")

# ── Local imports ─────────────────────────────────────────────────────────────

from agent.memory import Memory
from agent.core import FridayAgent
from agent.permissions import PermissionGate
from channels.groupme import GroupMeChannel
from channels.telegram import TelegramChannel
from channels.apple_calendar import AppleCalendarChannel


# ── Config loader ─────────────────────────────────────────────────────────────

def load_config(path: str = "friday_config.yaml") -> dict:
    if not os.path.exists(path):
        logger.critical(f"Config file not found: {path}")
        sys.exit(1)
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ── Startup checks ────────────────────────────────────────────────────────────

def check_environment(config: dict):
    errors = []

    if config.get("provider", "gemini") == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        errors.append("GEMINI_API_KEY environment variable not set.")

    groupme_cfg = config.get("groupme", {})
    if groupme_cfg.get("enabled") and not groupme_cfg.get("api_token"):
        errors.append("GroupMe is enabled but 'api_token' is empty in friday_config.yaml.")

    tg_cfg = config.get("telegram", {})
    tg_token = tg_cfg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat  = tg_cfg.get("chat_id")   or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not tg_token:
        errors.append("Telegram bot_token is not set (friday_config.yaml or TELEGRAM_BOT_TOKEN).")
    if not tg_chat:
        errors.append("Telegram chat_id is not set (friday_config.yaml or TELEGRAM_CHAT_ID).")

    if errors:
        for e in errors:
            logger.error(f"⚠ Config error: {e}")
        sys.exit(1)


# ── State file ────────────────────────────────────────────────────────────────

def write_state(agent: "FridayAgent", config: dict, started_at: str):
    """Atomically write runtime stats to state.json for the dashboard to read."""
    data = {
        "status": "running",
        "pid": os.getpid(),
        "provider": config.get("provider", "gemini"),
        "model": agent.model_name,
        "started_at": started_at,
        "last_poll_at": datetime.now().isoformat(),
        **agent._stats,
    }
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.warning(f"Could not write state.json: {e}")


# ── Poll cycle ────────────────────────────────────────────────────────────────

def run_poll_cycle(agent: FridayAgent, groupme: GroupMeChannel, config: dict,
                   started_at: str = ""):
    """Periodic GroupMe observation cycle. iMessage is handled by the watchdog."""
    logger.info("── Poll cycle starting ──")

    groupme_cfg = config.get("groupme", {})
    if groupme_cfg.get("enabled"):
        try:
            messages = groupme.poll()
            agent.process_groupme_messages(messages, groupme_cfg, groupme_channel=groupme)
        except Exception as e:
            logger.error(f"GroupMe poll error: {e}")

    write_state(agent, config, started_at)
    logger.info("── Poll cycle complete ──")


# ── Scheduler thread ──────────────────────────────────────────────────────────

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 50)
    logger.info("  Project Friday — Phase 2 Starting up")
    logger.info(f"  {datetime.now().strftime('%A, %B %d %Y %H:%M')}")
    logger.info("=" * 50)

    config = load_config()
    agent_cfg = config.get("agent", {})
    check_environment(config)

    # Initialize memory
    memory_cfg = config.get("memory", {})
    memory = Memory(memory_cfg.get("db_path", "memory/friday_memory.db"))

    # Initialize channels
    telegram = TelegramChannel(config=config.get("telegram", {}), memory=memory)

    groupme = GroupMeChannel(config=config.get("groupme", {}), memory=memory)

    calendar_cfg = config.get("calendars", {}).get("apple", {})
    calendar = AppleCalendarChannel(config=calendar_cfg)

    # Initialize agent
    agent = FridayAgent(
        config=config,
        memory=memory,
        telegram_channel=telegram,
        calendar_channel=calendar
    )

    # Initialize permission gate and inject into agent
    permissions = PermissionGate(
        memory=memory,
        telegram_channel=telegram,
        agent_core=agent,
        calendar_channel=calendar
    )
    agent.set_permissions(permissions)

    started_at = datetime.now().isoformat()

    # Start Telegram polling — delivers incoming messages to process_telegram_message()
    telegram.start_polling(agent.process_telegram_message)

    # Startup message
    cal_status = "✓ Apple Calendar" if calendar_cfg.get("enabled") else "✗ Calendar (disabled)"
    startup_msg = (
        f"⚡ Friday is online. 🟢 [Phase 2]\n"
        f"GroupMe: {', '.join(config.get('groupme', {}).get('approved_groups', []))}\n"
        f"{cal_status}\n"
        f"Telegram: ✓\n"
        f"Permission gate: ✓\n"
        f"Briefing: {agent_cfg.get('briefing_time', '21:00')}"
    )
    logger.info(startup_msg)
    telegram.send(startup_msg)

    # Write initial state so the dashboard shows "Running" immediately
    write_state(agent, config, started_at)

    # Schedule poll cycle
    interval_mins = agent_cfg.get("poll_interval_seconds", 300) // 60
    schedule.every(interval_mins).minutes.do(
        run_poll_cycle, agent=agent, groupme=groupme, config=config,
        started_at=started_at
    )

    # Schedule evening briefing
    briefing_time = agent_cfg.get("briefing_time", "21:00")
    schedule.every().day.at(briefing_time).do(agent.send_evening_briefing)
    logger.info(f"Evening briefing scheduled at {briefing_time}")

    # Run one poll immediately
    run_poll_cycle(agent, groupme, config, started_at=started_at)

    # Start scheduler thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    logger.info("Friday is running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Friday shutting down.")
        telegram.send("Friday is going offline. 🔴")
        telegram.stop_polling()


if __name__ == "__main__":
    main()
