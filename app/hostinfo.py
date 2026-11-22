"""Host-level numbers for the dashboard ring: memory, disk, and how much of each is Minecraft.

/proc/meminfo inside the container reports the Unraid host's memory (no lxcfs on Unraid),
so "available" here is what the whole server has free, not just this container.
Folder sizes are slow to add up, so they're refreshed in the background every 10 minutes.
"""
import os
import shutil
import threading
import time
from collections import deque

from . import config

_sizes = {"worlds_bytes": None, "backups_bytes": None, "at": 0.0}
_lock = threading.Lock()
SIZE_EVERY_S = 600

# per-server history for the small graphs: one sample per stats tick (10 s), last hour
HISTORY_LEN = 360
_hist: dict[str, deque] = {}


def meminfo(path: str = "/proc/meminfo") -> dict:
    vals = {}
    try:
        with open(path) as f:
            for line in f:
                k, v = line.split(":", 1)
                vals[k] = int(v.split()[0]) * 1024
    except OSError:
        return {"total_gb": None, "available_gb": None}
    gb = lambda b: round(b / 2**30, 1)
    return {"total_gb": gb(vals.get("MemTotal", 0)), "available_gb": gb(vals.get("MemAvailable", 0))}


def folder_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _refresh_sizes() -> None:
    per = {}
    try:
        for name in os.listdir(config.DATA_DIR):
            full = os.path.join(config.DATA_DIR, name)
            if not name.startswith(".") and os.path.isdir(full):
                per[name] = folder_bytes(full)
    except OSError:
        pass
    w = sum(per.values())
    b = folder_bytes(config.BACKUP_DIR) if os.path.isdir(config.BACKUP_DIR) else 0
    with _lock:
        _sizes.update(worlds_bytes=w, backups_bytes=b, per=per, at=time.time())


def server_gb(sid: str) -> float | None:
    b = (_sizes.get("per") or {}).get(sid)
    return None if b is None else round(b / 2**30, 1)


def maybe_refresh_sizes() -> None:
    if time.time() - _sizes["at"] > SIZE_EVERY_S:
        _sizes["at"] = time.time()          # claim it so only one refresh runs
        threading.Thread(target=_refresh_sizes, daemon=True).start()


def disk() -> dict:
    try:
        u = shutil.disk_usage(config.DATA_DIR)
    except OSError:
        return {}
    tb = lambda b: round(b / 2**40, 3)
    gbs = lambda b: None if b is None else round(b / 2**30, 1)
    return {"total_tb": tb(u.total), "free_tb": tb(u.free), "used_tb": tb(u.used),
            "minecraft_gb": gbs((_sizes["worlds_bytes"] or 0) + (_sizes["backups_bytes"] or 0))
            if _sizes["worlds_bytes"] is not None else None}


_guard = {"level": "ok", "where": "", "free_gb": None}


def check_disks() -> dict:
    """Free space where SpawnPoint writes (worlds, backups, the private engine). The lowest
    one decides: "low" warns, "critical" pauses backups, pre-generation and map drawing so a
    full disk can't corrupt a world mid-save."""
    from . import alerts
    worst = None
    seen = set()
    for label, path in (("worlds", config.DATA_DIR), ("backups", config.BACKUP_DIR), ("engine", "/var/lib/docker")):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        if (st.f_fsid, st.f_blocks) in seen:
            continue
        seen.add((st.f_fsid, st.f_blocks))
        free = st.f_bavail * st.f_frsize / 2**30
        if worst is None or free < worst[1]:
            worst = (label, free)
    if worst is None:
        return dict(_guard)
    label, free = worst
    level = "critical" if free < config.DISK_CRITICAL_GB else "low" if free < config.DISK_LOW_GB else "ok"
    if level != "ok" and level != _guard["level"]:
        alerts.send("disk_low", "", f"Disk {'nearly full' if level == 'critical' else 'running low'}: {free:.1f} GB left",
                    f"The {label} disk has {free:.1f} GB free." + (" Backups, pre-generation and map drawing are paused until there's more room."
                                                                     if level == "critical" else " Delete old backups or worlds, or add space."),
                    "bad" if level == "critical" else "warn")
    _guard.update(level=level, where=label, free_gb=round(free, 1))
    return dict(_guard)


def disk_level() -> str:
    return _guard["level"]


def disk_guard() -> dict:
    return dict(_guard)


# Longer history: one summary per 5 minutes for 7 days, saved so it survives restarts.
BUCKET_S = 300
LONG_LEN = 7 * 24 * 3600 // BUCKET_S
_long: dict[str, deque] = {}
_open: dict[str, dict] = {}          # the 5-minute bucket being filled


def _long_path(sid: str) -> str:
    return os.path.join(config.STATE_DIR, "history", f"{sid}.json")


def _load_long(sid: str) -> deque:
    d = _long.get(sid)
    if d is None:
        d = deque(maxlen=LONG_LEN)
        try:
            import json
            with open(_long_path(sid)) as f:
                d.extend(tuple(x) for x in json.load(f))
        except (OSError, ValueError):
            pass
        _long[sid] = d
    return d


def _save_long(sid: str) -> None:
    import json
    try:
        os.makedirs(os.path.dirname(_long_path(sid)), exist_ok=True)
        tmp = _long_path(sid) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(list(_long[sid]), f, separators=(",", ":"))
        os.replace(tmp, _long_path(sid))
    except OSError:
        pass


def record(sid: str, mspt: float | None, mem_gb: float, cpu: float, p95: float | None = None,
           players: int | None = None, now: float | None = None) -> None:
    now = now or time.time()
    d = _hist.setdefault(sid, deque(maxlen=HISTORY_LEN))
    d.append((round(now), mspt, mem_gb, cpu, p95, players))
    start = int(now // BUCKET_S * BUCKET_S)
    b = _open.get(sid)
    if b and b["t"] != start:
        _close(sid, b)
        b = None
    if b is None:
        b = _open[sid] = {"t": start, "n": 0, "mspt": [], "p95": [], "mem": [], "cpu": [], "players": []}
    b["n"] += 1
    for k, v in (("mspt", mspt), ("p95", p95), ("mem", mem_gb), ("cpu", cpu), ("players", players)):
        if v is not None:
            b[k].append(v)


def _close(sid: str, b: dict) -> None:
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    mx = lambda xs: round(max(xs), 2) if xs else None
    # (time, average tick, worst 95th-percentile tick, average memory, peak memory, average CPU, most players)
    _load_long(sid).append((b["t"], avg(b["mspt"]), mx(b["p95"]), avg(b["mem"]), mx(b["mem"]), avg(b["cpu"]), mx(b["players"])))
    _save_long(sid)


def history(sid: str) -> list:
    return list(_hist.get(sid, ()))


def long_history(sid: str, since: float) -> list:
    return [x for x in _load_long(sid) if x[0] >= since]


def forget(sid: str) -> None:
    _hist.pop(sid, None)
    _long.pop(sid, None)
    _open.pop(sid, None)
    try:
        os.remove(_long_path(sid))
    except OSError:
        pass
