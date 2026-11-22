"""Tiny JSON-file state store. One admin, a handful of servers: no database needed."""
import json
import os
import re
import threading
import time
from typing import Any

from . import config

_lock = threading.RLock()
_path = os.path.join(config.STATE_DIR, "state.json")
_state: dict[str, Any] = {"servers": {}}


def load() -> None:
    global _state
    os.makedirs(config.STATE_DIR, exist_ok=True)
    if os.path.exists(_path):
        with open(_path) as f:
            _state = json.load(f)
    _state.setdefault("servers", {})


def save() -> None:
    with _lock:
        tmp = _path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state, f, indent=2)
        os.replace(tmp, _path)


def servers() -> dict[str, dict]:
    return _state["servers"]


def get(sid: str) -> dict:
    s = _state["servers"].get(sid)
    if not s:
        raise KeyError(sid)
    return s


def put(server: dict) -> None:
    with _lock:
        _state["servers"][server["id"]] = server
        save()


def update(sid: str, **fields) -> dict:
    with _lock:
        s = get(sid)
        s.update(fields)
        save()
        return s


def delete(sid: str) -> None:
    with _lock:
        _state["servers"].pop(sid, None)
        save()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:40].strip("-")
    return s or f"server-{int(time.time())}"


def unique_id(name: str) -> str:
    base = slugify(name)
    sid, n = base, 2
    while sid in _state["servers"]:
        sid = f"{base}-{n}"
        n += 1
    return sid
