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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_HERE, "logs", "friday.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("friday")

from agent.core import FridayAgent
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
        errors.append("telegram.bot_token not set.")
    if not (tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")):
        errors.append("telegram.chat_id not set.")
    if config.get("provider") == "gemini":
        if not (config.get("gemini", {}).get("api_key") or os.environ.get("GEMINI_API_KEY")):
            errors.append("GEMINI_API_KEY not set.")
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


def main() -> None:
    logger.info("=" * 50)
    logger.info("  Project Friday — Starting up")
    logger.info(f"  {datetime.now().strftime('%A, %B %d %Y %H:%M')}")
    logger.info("=" * 50)

    config = load_config()
    check_environment(config)

    telegram = TelegramChannel(config=config.get("telegram", {}))
    agent = FridayAgent(config=config, telegram=telegram)

    started_at = datetime.now().isoformat()
    telegram.start_polling(agent.on_message)
    write_state(agent, config, started_at)

    schedule.every(5).minutes.do(write_state, agent=agent, config=config, started_at=started_at)
    scheduler = threading.Thread(target=lambda: [schedule.run_pending() or time.sleep(30) for _ in iter(int, 1)], daemon=True)
    scheduler.start()

    telegram.send(f"⚡ Friday online — {config.get('provider', 'ollama')} / {agent.model_name}")

    logger.info("Running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        telegram.send("Friday going offline. 🔴")
        telegram.stop_polling()


if __name__ == "__main__":
    main()
