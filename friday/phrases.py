import random
import os
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_QUIPS_FILE = os.path.join(_HERE, "quips.yaml")

def random_quip() -> str:
    try:
        with open(_QUIPS_FILE) as f:
            data = yaml.safe_load(f)
        quips = data.get("confirm_quips", [])
        if quips:
            return random.choice(quips)
    except Exception:
        pass
    return "As you wish, sir."
