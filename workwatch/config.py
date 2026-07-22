"""Configuration and history file management for WorkWatch."""

import json
from datetime import datetime, time
from pathlib import Path

CONFIG_PATH = Path.home() / ".workwatch.json"
HISTORY_PATH = Path.home() / ".workwatch_history.json"

HALF_DAY_CUTOFF = time(14, 0)

DEFAULT_CONFIG = {
    "work_hours": 9,
    "half_day_hours": 4.5,
    "sender": "attendance@vmock.com",
    "overtime_enabled": True,
    "inactive_threshold_minutes": 10,
    "give_up_after": "16:00",     # stop waiting for the attendance mail past
                                  # this local time (leave/holiday) and exit
    "archive_email": "",
    # `recap` command settings ----------------------------------------
    "recap_repos": [],            # explicit dirs to scan (do NOT auto-scan $HOME)
    "recap_author": "",           # git --author filter; "" → git config user.email
    "recap_default_window": "24h",
    "recap_include_claude": True,  # include the Claude Code "Explored" bucket
    "recap_ai_review": False,      # reserved: Claude-API verdict instead of rules
}


def get_effective_work_hours(entry_time: datetime, config: dict) -> float:
    """Return half_day_hours if entry is at/after 2:00 PM, else work_hours."""
    if entry_time.time() >= HALF_DAY_CUTOFF:
        return config["half_day_hours"]
    return config["work_hours"]


def load_config() -> dict:
    """Load config from ~/.workwatch.json, creating defaults if missing."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        print(f"📄 Created default config at {CONFIG_PATH}")
        print(f"   Edit work_hours or sender if needed.\n")
        return dict(DEFAULT_CONFIG)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    # Merge with defaults for any missing keys, and persist additions
    # so the on-disk file stays in sync with DEFAULT_CONFIG.
    missing = [k for k in DEFAULT_CONFIG if k not in config]
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)
    if missing:
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
        print(f"📄 Added new config keys to {CONFIG_PATH}: {', '.join(missing)}")

    return config


def load_history() -> dict:
    """Load attendance history from ~/.workwatch_history.json."""
    if not HISTORY_PATH.exists():
        return {}

    with open(HISTORY_PATH) as f:
        return json.load(f)


def save_history(history: dict) -> None:
    """Save attendance history to ~/.workwatch_history.json."""
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)
        f.write("\n")
