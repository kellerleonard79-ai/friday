"""
friday.py
Project Friday — entry point.
"""

import json
import logging
import os
import sys
import time
import threading
from datetime import datetime

import yaml
import schedule

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "state.json")

os.makedirs(os.path.join(_HERE, "logs"), exist_ok=True)
os.makedirs(os.path.join(_HERE, "memory"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_HERE, "logs", "friday.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("friday")

from agent.memory import Memory
from agent.core import FridayAgent
from agent.permissions import PermissionGate
from channels.telegram import TelegramChannel


def load_config() -> dict:
    path = os.path.join(_HERE, "friday_config.yaml")
    if not os.path.exists(path):
        logger.critical(f"Config not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def check_environment(config: dict) -> None:
    errors = []
    tg = config.get("telegram", {})
    if not (tg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")):
        errors.append("telegram.bot_token not set (config or TELEGRAM_BOT_TOKEN env var).")
    if not (tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")):
        errors.append("telegram.chat_id not set (config or TELEGRAM_CHAT_ID env var).")
    if config.get("provider") == "gemini":
        if not (config.get("gemini", {}).get("api_key") or os.environ.get("GEMINI_API_KEY")):
            errors.append("Gemini provider selected but GEMINI_API_KEY not set.")
    for e in errors:
        logger.error(f"Config error: {e}")
    if errors:
        sys.exit(1)


def write_state(agent: FridayAgent, config: dict, started_at: str) -> None:
    data = {
        "status": "running",
        "pid": os.getpid(),
        "provider": config.get("provider", "ollama"),
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


def run_scheduler() -> None:
    while True:
        schedule.run_pending()
        time.sleep(30)


def main() -> None:
    logger.info("=" * 50)
    logger.info("  Project Friday — Starting up")
    logger.info(f"  {datetime.now().strftime('%A, %B %d %Y %H:%M')}")
    logger.info("=" * 50)

    config = load_config()
    check_environment(config)

    memory_cfg = config.get("memory", {})
    db_path = os.path.join(_HERE, memory_cfg.get("db_path", "memory/friday_memory.db"))
    memory = Memory(db_path)

    telegram = TelegramChannel(config=config.get("telegram", {}), memory=memory)
    agent = FridayAgent(config=config, memory=memory, telegram=telegram)

    permissions = PermissionGate(memory=memory, telegram=telegram, agent=agent)
    agent.set_permissions(permissions)

    started_at = datetime.now().isoformat()

    telegram.start_polling(agent.on_message)

    briefing_time = config.get("agent", {}).get("briefing_time", "21:45")
    schedule.every().day.at(briefing_time).do(agent.send_evening_briefing)
    logger.info(f"Evening briefing scheduled at {briefing_time}")

    write_state(agent, config, started_at)

    schedule.every(5).minutes.do(write_state, agent=agent, config=config, started_at=started_at)

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    telegram.send(
        f"⚡ Friday is online.\n"
        f"Provider: {config.get('provider', 'ollama')} / {agent.model_name}\n"
        f"Briefing: {briefing_time}"
    )

    logger.info("Friday is running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        telegram.send("Friday is going offline. 🔴")
        telegram.stop_polling()


if __name__ == "__main__":
    main()
