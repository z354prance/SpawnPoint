"""Scheduled tasks per server: backups, restarts and stops with in-game warnings, starts,
and console commands. Times are in the container's time zone (TZ).

A task looks like:
  {"id": "a1b2c3", "kind": "restart", "enabled": true,
   "when": {"type": "daily", "time": "05:00", "days": [0, 1, 2, 3, 4, 5, 6]},   # 0 = Monday
   "warn_min": 5, "backup_first": true}
  {"id": "...", "kind": "backup", "when": {"type": "hours", "hours": 6, "minute": 0}}
  {"id": "...", "kind": "command", "command": "say hello", "when": {...}}
"""
import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import activity, config, dockerctl, rcon, state

log = logging.getLogger("spawnpoint.schedule")

KINDS = ("backup", "restart", "stop", "start", "command")
WARN_AT = (900, 600, 300, 120, 60, 30, 10, 5, 4, 3, 2, 1)     # seconds before, when players are told
LATE_OK_S = 10 * 60          # run a task up to this late (e.g. SpawnPoint was restarting); older ones are skipped
TICK_S = 15
_running: dict[tuple[str, str], asyncio.Task] = {}


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.TZ)
    except Exception:
        return ZoneInfo("UTC")


# ------------------------------------------------------------------ validation
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def clean(task: dict) -> dict:
    """Check and normalise a task from the dashboard. Raises ValueError with a readable message."""
    kind = task.get("kind")
    if kind not in KINDS:
        raise ValueError("Pick what the task does")
    w = task.get("when") or {}
    if w.get("type") == "daily":
        m = _TIME.match(str(w.get("time", "")))
        if not m:
            raise ValueError("Use a time like 05:00")
        days = sorted({int(d) for d in (w.get("days") or range(7)) if 0 <= int(d) <= 6})
        if not days:
            raise ValueError("Pick at least one day")
        when = {"type": "daily", "time": f"{int(m.group(1)):02d}:{m.group(2)}", "days": days}
    elif w.get("type") == "hours":
        hours = int(w.get("hours") or 0)
        if hours not in (1, 2, 3, 4, 6, 8, 12):
            raise ValueError("Every 1, 2, 3, 4, 6, 8 or 12 hours")
        when = {"type": "hours", "hours": hours, "minute": max(0, min(59, int(w.get("minute") or 0)))}
    else:
        raise ValueError("Pick when it runs")
    out = {"id": str(task.get("id") or secrets.token_hex(4))[:16], "kind": kind, "when": when,
           "enabled": task.get("enabled", True) is not False}
    if kind in ("restart", "stop"):
        out["warn_min"] = max(0, min(15, int(task.get("warn_min") or 0)))
    if kind == "restart":
        out["backup_first"] = bool(task.get("backup_first"))
    if kind == "command":
        cmd = str(task.get("command") or "").strip().lstrip("/")
        if not cmd or len(cmd) > 400 or "\n" in cmd:
            raise ValueError("Enter one console command")
        out["command"] = cmd
    for k in ("last_run", "last_result", "last_ok"):
        if k in task:
            out[k] = task[k]
    return out


# ------------------------------------------------------------------ timing
def occurrences(when: dict, start: datetime, end: datetime):
    """Scheduled times in (start, end], in the server time zone."""
    z = tz()
    start, end = start.astimezone(z), end.astimezone(z)
    day = start.date() - timedelta(days=1)
    while day <= end.date():
        if when["type"] == "daily":
            hh, mm = map(int, when["time"].split(":"))
            times = [(hh, mm)] if day.weekday() in when["days"] else []
        else:
            times = [(h, when.get("minute", 0)) for h in range(0, 24, when["hours"])]
        for hh, mm in times:
            t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=z)
            if start < t <= end:
                yield t
        day += timedelta(days=1)


def next_run(task: dict, now: float | None = None) -> float | None:
    now_dt = datetime.fromtimestamp(now or time.time(), tz())
    for t in occurrences(task["when"], now_dt, now_dt + timedelta(days=8)):
        return t.timestamp()
    return None


def due(task: dict, now: float) -> float | None:
    """The scheduled time this task should act on now, if any. Tasks with a warning
    start that many minutes early, so the restart itself happens on the hour."""
    if task.get("enabled") is False:
        return None
    lead = task.get("warn_min", 0) * 60 if task["kind"] in ("restart", "stop") else 0
    last = task.get("last_run") or (now - LATE_OK_S)
    hits = list(occurrences(task["when"], datetime.fromtimestamp(max(last, now - LATE_OK_S - lead), tz()),
                            datetime.fromtimestamp(now + lead, tz())))
    if not hits:
        return None
    t = hits[-1].timestamp()
    return t if t > (task.get("last_run") or 0) else None


# ------------------------------------------------------------------ running
async def _say(s: dict, text: str) -> None:
    from . import servers
    payload = '{"text":"[Server] ' + text.replace('"', "'") + '","color":"gold"}'
    try:
        await servers.arcmd(s, f"tellraw @a {payload}")
    except rcon.RconError:
        pass


def _left(sec: int) -> str:
    if sec >= 60:
        m = sec // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{sec} second{'s' if sec != 1 else ''}"


async def _countdown(sid: str, at: float, what: str) -> None:
    """Tell players what's coming at each step of WARN_AT, until the scheduled time."""
    for step in WARN_AT:
        wait = at - step - time.time()
        if wait < -1:
            continue
        if wait > 0:
            await asyncio.sleep(wait)
        s = state.get(sid)
        if not s or dockerctl.status(sid) != "running":
            return
        await _say(s, f"{what} in {_left(step)}")
    await asyncio.sleep(max(0, at - time.time()))


async def run_task(sid: str, task: dict, at: float | None = None) -> tuple[bool, str]:
    """Do one task now (or at `at`, after warnings). Returns (ok, what happened)."""
    from . import backups, servers
    s = state.get(sid)
    if not s:
        return False, "The server no longer exists"
    kind = task["kind"]
    up = dockerctl.status(sid) == "running" and s.get("phase") in ("online", "pregen-spawn")
    if kind == "backup":
        if s.get("first_boot"):
            return False, "Skipped: still installing"
        r = await backups.create(sid, "scheduled")
        return True, f"Backed up ({_size(r.get('added'))} new)"
    if kind == "start":
        if up or s.get("phase") in ("starting", "installing", "pregen-spawn"):
            return True, "Already running"
        await servers.start(sid)
        return True, "Started"
    if not up:
        return True, "Skipped: the server wasn't running"
    if kind == "command":
        out = await servers.arcmd(s, task["command"])
        return True, (out.strip()[:200] or "Sent")
    what = "Restarting" if kind == "restart" else "Stopping"
    if task.get("warn_min") and at:
        await _countdown(sid, at, f"The server is {'restarting' if kind == 'restart' else 'stopping'}")
    if kind == "restart" and task.get("backup_first"):
        try:
            await backups.create(sid, "before-restart")
        except Exception as e:
            log.warning("[%s] backup before restart failed: %s", sid, e)
    if kind == "restart":
        await servers.restart(sid)
        return True, "Restarted"
    await servers.stop(sid)
    return True, "Stopped"


def _size(n) -> str:
    if not n:
        return "nothing"
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{max(1, n // 2**20)} MB" if n >= 2**20 else f"{max(1, n // 1024)} KB"


def _record(sid: str, tid: str, **fields) -> None:
    s = state.get(sid)
    if not s:
        return
    tasks = [dict(t, **fields) if t["id"] == tid else t for t in s.get("schedule", [])]
    state.update(sid, schedule=tasks)


async def _go(sid: str, task: dict, at: float | None, by: str = "Schedule") -> None:
    try:
        ok, msg = await run_task(sid, task, at)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        ok, msg = False, str(e)[:200] or e.__class__.__name__
    _record(sid, task["id"], last_result=msg, last_ok=ok, last_done=time.time())
    if not ok and task["kind"] != "backup":          # failed backups already send their own alert
        from . import alerts
        name = (state.get(sid) or {}).get("name", sid)
        alerts.send("schedule_failed", name, f"Scheduled task failed on {name}", f"{describe(task)}: {msg}", "bad")
    activity.record(by, "system" if by == "Schedule" else "", "", "schedule.run", sid, f"{describe(task)}: {msg}", ok)
    log.info("[%s] schedule %s: %s", sid, task["kind"], msg)


def start_now(sid: str, task: dict, by: str) -> bool:
    key = (sid, task["id"])
    if key in _running and not _running[key].done():
        return False
    _running[key] = asyncio.create_task(_go(sid, dict(task, warn_min=0), None, by))
    return True


def describe(task: dict) -> str:
    w = task["when"]
    if w["type"] == "daily":
        days = w["days"]
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        d = "every day" if len(days) == 7 else "weekdays" if days == [0, 1, 2, 3, 4] else \
            "weekends" if days == [5, 6] else ", ".join(names[i] for i in days)
        when = f"{d} at {w['time']}"
    else:
        when = f"every {w['hours']} hour{'s' if w['hours'] != 1 else ''}"
    what = {"backup": "Back up", "restart": "Restart", "stop": "Stop", "start": "Start",
            "command": f"Run “{task.get('command', '')}”"}[task["kind"]]
    return f"{what} {when}"


async def tick(now: float | None = None) -> None:
    now = now or time.time()
    for sid, s in list(state.servers().items()):
        for task in s.get("schedule") or []:
            key = (sid, task["id"])
            if key in _running and not _running[key].done():
                continue
            at = due(task, now)
            if at is None:
                continue
            _record(sid, task["id"], last_run=at)
            _running[key] = asyncio.create_task(_go(sid, task, at))


async def loop() -> None:
    await asyncio.sleep(10)
    while True:
        try:
            await tick()
        except Exception as e:
            log.warning("schedule loop: %s", e)
        await asyncio.sleep(TICK_S)


def stop_all() -> None:
    for t in _running.values():
        t.cancel()
