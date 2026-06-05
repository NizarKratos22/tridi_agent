"""
Lightweight heartbeat file — agents write here every cycle,
dashboard reads it to show live connection status.
"""
import json
import os
from datetime import datetime, timezone

STATUS_PATH = os.path.join(os.path.dirname(__file__), "status.json")


def _load() -> dict:
    try:
        with open(STATUS_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    with open(STATUS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def beat(service: str, ok: bool, detail: str = ""):
    """Called by agents to record their last heartbeat."""
    data = _load()
    data[service] = {
        "ok":     ok,
        "detail": detail,
        "ts":     datetime.now(timezone.utc).isoformat(),
    }
    _save(data)


def read_all() -> dict:
    return _load()
