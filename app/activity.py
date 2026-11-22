"""Activity log: who signed in (and failed to), and who did what.

One JSON object per line in <state dir>/activity.log, rotated at 5 MB (one old file
kept as activity.log.1). Entries: ts, user, role, ip, action, target, detail, ok.
"""
import json
import logging
import os
import threading
import time

from . import config

_lock = threading.Lock()
log = logging.getLogger("spawnpoint.activity")
MAX_BYTES = 5 * 2**20


def _path() -> str:
    return os.path.join(config.STATE_DIR, "activity.log")


def record(user: str | None, role: str | None, ip: str | None, action: str,
           target: str = "", detail: str = "", ok: bool = True) -> dict:
    e = {"ts": round(time.time(), 3), "user": user or "", "role": role or "", "ip": ip or "",
         "action": action, "target": target or "", "detail": (detail or "")[:500], "ok": ok}
    p = _path()
    try:                                    # a full or read-only disk must never break the action itself
        with _lock:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if os.path.exists(p) and os.path.getsize(p) > MAX_BYTES:
                os.replace(p, p + ".1")
            with open(p, "a") as f:
                f.write(json.dumps(e) + "\n")
    except OSError as err:
        log.warning("could not write activity log: %s (%s)", err, e)
    return e


def read(limit: int = 200, user: str = "", action: str = "", target: str = "", before: float | None = None) -> list[dict]:
    """Newest first, filtered."""
    out: list[dict] = []
    for p in (_path(), _path() + ".1"):
        if not os.path.exists(p):
            continue
        with open(p) as f:
            lines = f.readlines()
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if before and e["ts"] >= before:
                continue
            if user and e.get("user") != user:
                continue
            if action and not e.get("action", "").startswith(action):
                continue
            if target and e.get("target") != target:
                continue
            out.append(e)
            if len(out) >= limit:
                return out
    return out
