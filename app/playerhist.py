"""Player history: first and last seen, playtime, sessions, and where people spend time.

The stats loop calls note() every ~10 s with who's online, and sample() about once a
minute with where they are. Positions are counted in 32-block cells, per dimension and
per player, which is enough for a heat layer on the map and a "usually around" line.

Kept in STATE_DIR/players/<server>.json, written at most once a minute."""
from __future__ import annotations

import json
import os
import threading
import time

from . import config

CELL = 32                       # blocks per heat cell
MAX_CELLS = 400                 # per player and dimension; the quietest are dropped past this
GAP_S = 60                      # missing from the list this long ends a session
SAVE_EVERY_S = 60

_data: dict[str, dict] = {}
_dirty: dict[str, float] = {}
_lock = threading.Lock()


def _path(sid: str) -> str:
    return os.path.join(config.STATE_DIR, "players", f"{sid}.json")


def _get(sid: str) -> dict:
    if sid not in _data:
        try:
            with open(_path(sid)) as f:
                _data[sid] = json.load(f)
        except (OSError, ValueError):
            _data[sid] = {}
        _data[sid].setdefault("players", {})
        _data[sid].setdefault("heat", {})
    return _data[sid]


def _flush(sid: str, now: float, force: bool = False) -> None:
    if sid not in _dirty or (not force and now - _dirty[sid] < SAVE_EVERY_S):
        return
    os.makedirs(os.path.dirname(_path(sid)), exist_ok=True)
    tmp = _path(sid) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_data[sid], f, separators=(",", ":"))
    os.replace(tmp, _path(sid))
    _dirty.pop(sid, None)


def _touch(sid: str, now: float) -> None:
    _dirty.setdefault(sid, now)


def note(sid: str, names: list[str], now: float | None = None) -> None:
    """Who's online right now."""
    now = now or time.time()
    with _lock:
        d = _get(sid)
        for name in names:
            p = d["players"].setdefault(name, {"first": now, "last": now, "play_s": 0.0, "sessions": 0})
            p.setdefault("seen", 0)
            gap = now - p.get("seen", 0)
            if gap > GAP_S:
                p["sessions"] += 1                   # (re)joined
            else:
                p["play_s"] += gap
            p["seen"] = p["last"] = now
        if names:
            _touch(sid, now)
        _flush(sid, now)


def sample(sid: str, positions: list[dict], now: float | None = None) -> None:
    """Where online players are (x, z, dim), counted into heat cells."""
    now = now or time.time()
    with _lock:
        d = _get(sid)
        for pos in positions:
            key = f"{int(pos['x']) // CELL},{int(pos['z']) // CELL}"
            for bucket in (d["heat"].setdefault(pos["dim"], {}),
                           d["players"].setdefault(pos["name"], {"first": now, "last": now, "play_s": 0.0, "sessions": 1, "seen": now})
                           .setdefault("cells", {}).setdefault(pos["dim"], {})):
                bucket[key] = bucket.get(key, 0) + 1
                if len(bucket) > MAX_CELLS * 4:
                    for k in sorted(bucket, key=bucket.get)[: len(bucket) - MAX_CELLS * 3]:
                        del bucket[k]
        if positions:
            _touch(sid, now)
        _flush(sid, now)


def players(sid: str, online: list[str] | None = None) -> list[dict]:
    """Everyone who has played, most recent first."""
    online = set(online or [])
    with _lock:
        d = _get(sid)
        out = []
        for name, p in d["players"].items():
            home, best = None, 0
            for dim, cells in (p.get("cells") or {}).items():
                for k, n in cells.items():
                    if n > best:
                        best, home = n, (dim, *[int(c) * CELL + CELL // 2 for c in k.split(",")])
            out.append({"name": name, "first": p["first"], "last": p["last"], "play_s": round(p["play_s"]),
                        "sessions": p["sessions"], "online": name in online,
                        "home": {"dim": home[0], "x": home[1], "z": home[2]} if home else None})
    return sorted(out, key=lambda r: (not r["online"], -r["last"]))


def heat(sid: str, dim: str) -> dict:
    with _lock:
        cells = dict(_get(sid)["heat"].get(dim, {}))
    return {"cell": CELL, "cells": [[*map(int, k.split(",")), n] for k, n in cells.items()]}


def forget(sid: str) -> None:
    with _lock:
        _data.pop(sid, None)
        _dirty.pop(sid, None)
        try:
            os.remove(_path(sid))
        except OSError:
            pass


def flush_all() -> None:
    with _lock:
        for sid in list(_dirty):
            _flush(sid, time.time(), force=True)
