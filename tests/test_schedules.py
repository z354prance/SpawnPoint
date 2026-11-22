import asyncio
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import config, dockerctl, main, schedules, servers, state, users

CHI = "America/Chicago"


def ts(y, mo, d, h, mi, s=0):
    from zoneinfo import ZoneInfo
    return datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo(CHI)).timestamp()


@pytest.fixture(autouse=True)
def chicago(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TZ", CHI)
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    state.put({"id": "aoc", "name": "aoc", "phase": "online", "pregen": {"stage": "done"}})


def test_clean_validates():
    t = schedules.clean({"kind": "restart", "when": {"type": "daily", "time": "5:00", "days": [0, 1, 9]}, "warn_min": 99})
    assert t["when"] == {"type": "daily", "time": "05:00", "days": [0, 1]} and t["warn_min"] == 15 and t["enabled"]
    for bad in ({"kind": "explode", "when": {"type": "daily", "time": "05:00"}},
                {"kind": "backup", "when": {"type": "daily", "time": "25:00"}},
                {"kind": "backup", "when": {"type": "hours", "hours": 5}},
                {"kind": "command", "command": "", "when": {"type": "hours", "hours": 6}}):
        with pytest.raises(ValueError):
            schedules.clean(bad)
    assert schedules.clean({"kind": "command", "command": "/say hi", "when": {"type": "hours", "hours": 6}})["command"] == "say hi"


def test_next_run_daily_weekdays_and_hours():
    sat_noon = ts(2026, 11, 21, 12, 0)                    # a Saturday
    weekdays = {"kind": "backup", "when": {"type": "daily", "time": "05:00", "days": [0, 1, 2, 3, 4]}}
    assert schedules.next_run(weekdays, sat_noon) == ts(2026, 11, 23, 5, 0)       # Monday
    six = {"kind": "backup", "when": {"type": "hours", "hours": 6, "minute": 30}}
    assert schedules.next_run(six, sat_noon) == ts(2026, 11, 21, 12, 30)
    assert schedules.describe(weekdays) == "Back up weekdays at 05:00"


def test_due_runs_once_and_starts_early_for_warnings():
    task = {"id": "r", "kind": "restart", "warn_min": 5, "when": {"type": "daily", "time": "05:00", "days": list(range(7))},
            "last_run": ts(2026, 11, 20, 5, 0)}
    assert schedules.due(task, ts(2026, 11, 21, 4, 50)) is None
    at = schedules.due(task, ts(2026, 11, 21, 4, 55, 5))   # warnings begin 5 minutes early
    assert at == ts(2026, 11, 21, 5, 0)
    task["last_run"] = at
    assert schedules.due(task, ts(2026, 11, 21, 5, 1)) is None
    # long missed runs (SpawnPoint was off for hours) are skipped, not replayed
    task["last_run"] = ts(2026, 11, 18, 5, 0)
    assert schedules.due(task, ts(2026, 11, 21, 9, 0)) is None
    task["enabled"] = False
    assert schedules.due(task, ts(2026, 11, 21, 4, 59)) is None


def test_restart_counts_down_then_restarts(monkeypatch):
    said, did = [], []

    async def arcmd(s, cmd):
        said.append(cmd); return ""
    async def restart(sid): did.append("restart")
    monkeypatch.setattr(servers, "arcmd", arcmd)
    monkeypatch.setattr(servers, "restart", restart)
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running")
    monkeypatch.setattr(schedules, "WARN_AT", (2, 1))
    task = {"id": "r", "kind": "restart", "warn_min": 1, "when": {"type": "hours", "hours": 1}}
    ok, msg = asyncio.run(schedules.run_task("aoc", task, time.time() + 2.2))
    assert ok and msg == "Restarted" and did == ["restart"]
    assert len(said) == 2 and "restarting in 2 seconds" in said[0] and said[0].startswith("tellraw @a")


def test_skips_when_not_running_and_tick_records(monkeypatch):
    monkeypatch.setattr(dockerctl, "status", lambda sid: "exited")
    task = {"id": "c", "kind": "command", "command": "say hi", "when": {"type": "hours", "hours": 1, "minute": 0}}
    ok, msg = asyncio.run(schedules.run_task("aoc", task))
    assert ok and "wasn't running" in msg
    now = (time.time() // 3600) * 3600 + 60              # a minute past the hour: the hourly task is due
    state.update("aoc", schedule=[dict(task, last_run=now - 7200)])

    async def go():
        await schedules.tick(now); await asyncio.gather(*[t for t in schedules._running.values()])
    asyncio.run(go())
    t = state.get("aoc")["schedule"][0]
    assert t["last_ok"] and "wasn't running" in t["last_result"] and t["last_run"] == now - 60


def test_schedule_api(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    users._fails.clear(); users.ensure_bootstrap()
    c = TestClient(main.app)
    assert c.get("/api/servers/aoc/schedule").status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
    r = c.put("/api/servers/aoc/schedule", json={"tasks": [
        {"kind": "backup", "when": {"type": "hours", "hours": 6}},
        {"kind": "restart", "warn_min": 5, "backup_first": True, "when": {"type": "daily", "time": "05:00", "days": [0, 1, 2, 3, 4, 5, 6]}}]})
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert [t["text"] for t in tasks] == ["Back up every 6 hours", "Restart every day at 05:00"]
    assert all(t["next_run"] > time.time() for t in tasks) and all(t["last_run"] for t in tasks)
    assert c.put("/api/servers/aoc/schedule", json={"tasks": [{"kind": "backup", "when": {"type": "daily", "time": "x"}}]}).status_code == 422
    assert c.post("/api/servers/aoc/schedule/nope/run").status_code == 404
