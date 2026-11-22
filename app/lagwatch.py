"""Automatic lag reports.

When a server's slowest ticks (95th percentile) stay over LAG_REPORT_MS for most of a few
minutes, SpawnPoint runs a 60-second spark profile and keeps the link it uploads, so the
cause of lag can be found afterwards without having to catch it live.
"""
import asyncio
import logging
import re
import time
from collections import deque

from . import activity, config, dockerctl, state

log = logging.getLogger("spawnpoint.lag")
WINDOW = 18                 # samples (10 s apart): 3 minutes
NEEDED = 12                 # this many of them over the line
COOLDOWN_S = 30 * 60
PROFILE_S = 60
NO_SPARK_RETRY_S = 6 * 3600
KEEP = 10
LINK = re.compile(r"https://spark\.lucko\.me/[A-Za-z0-9]+")
_recent: dict[str, deque] = {}
_running: set[str] = set()


def enabled(s: dict) -> bool:
    return s.get("lag_reports", True) is not False


def note(sid: str, p95: float | None, mspt: float | None, players: int | None) -> None:
    """Called by the stats loop every 10 s for online servers."""
    s = state.get(sid)
    if not s or p95 is None or not enabled(s):
        return
    d = _recent.setdefault(sid, deque(maxlen=WINDOW))
    d.append(p95 > config.LAG_REPORT_MS)
    if sid in _running or sum(d) < NEEDED:
        return
    last = max([r.get("at", 0) for r in s.get("lag_reports") or []] + [s.get("lag_tried", 0)])
    if time.time() - last < COOLDOWN_S:
        return
    if s.get("lag_no_spark") and time.time() - s["lag_no_spark"] < NO_SPARK_RETRY_S:
        return
    d.clear()
    _running.add(sid)
    asyncio.get_running_loop().create_task(_profile(sid, p95, mspt, players))


async def _profile(sid: str, p95: float, mspt: float | None, players: int | None) -> None:
    from . import alerts, servers
    try:
        state.update(sid, lag_tried=time.time())
        s = state.get(sid)
        seq = dockerctl.hub(sid).seq
        try:
            out = await servers.arcmd(s, f"spark profiler start --timeout {PROFILE_S}")
        except Exception as e:
            log.info("[%s] lag report: %s", sid, e)
            return
        if "Unknown" in out or "Incorrect" in out:
            state.update(sid, lag_no_spark=time.time())
            log.info("[%s] lag report skipped: spark isn't installed", sid)
            return
        url = LINK.search(out or "")
        deadline = time.time() + PROFILE_S + 90
        while not url and time.time() < deadline:
            await asyncio.sleep(3)
            h = dockerctl.hub(sid)
            for n, line in h.since(seq):
                seq = n
                url = url or LINK.search(line)
            if dockerctl.status(sid) != "running":
                return
        if not url:
            log.info("[%s] lag report: spark didn't give a link", sid)
            return
        rep = {"at": time.time(), "p95": round(p95, 1), "mspt": round(mspt, 1) if mspt is not None else None,
               "players": players, "url": url.group(0)}
        s = state.get(sid)
        state.update(sid, lag_reports=([rep] + (s.get("lag_reports") or []))[:KEEP])
        name = s.get("name", sid)
        activity.record("SpawnPoint", "system", "", "server.lag", sid, f"Slowest ticks {rep['p95']} ms. Report: {rep['url']}", False)
        alerts.send("lag_report", name, f"{name} is lagging: report recorded",
                    f"The slowest ticks were around {rep['p95']} ms (over {config.LAG_REPORT_MS} ms is noticeable)"
                    f"{f' with {players} online' if players else ''}. What was using the time: {rep['url']}", "warn")
        log.info("[%s] lag report: %s", sid, rep["url"])
    finally:
        _running.discard(sid)


def forget(sid: str) -> None:
    _recent.pop(sid, None)
