import asyncio

from app import activity, dockerctl, servers, state, topology


def _setup(monkeypatch, tmp_path, info, lines, **extra):
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    state.put({"id": "s", "name": "s", "phase": "online", "memory": {"init_gb": 2, "max_gb": 4},
               "pregen": {"stage": "done"}, **extra})
    monkeypatch.setattr(dockerctl, "exit_info", lambda sid: info)
    monkeypatch.setattr(dockerctl, "recent_logs", lambda sid, n=300: lines)
    monkeypatch.setattr(servers, "_ram_check", lambda s: None)
    monkeypatch.setattr(topology, "allocator", lambda: type("A", (), {"release": lambda self, sid: None})())
    started = []

    async def start(sid):
        started.append(sid)
        servers.set_phase(sid, "starting", "")
    monkeypatch.setattr(servers, "start", start)
    return started


def test_console_stop_is_not_a_crash(monkeypatch, tmp_path):
    started = _setup(monkeypatch, tmp_path, {"code": 0, "oom": False}, ["[Server thread/INFO]: Stopping server"])
    asyncio.run(servers.check_crash("s"))
    assert started == [] and state.get("s")["phase"] == "stopped"


def test_crash_restarts_and_is_logged(monkeypatch, tmp_path):
    started = _setup(monkeypatch, tmp_path, {"code": 0, "oom": False},
                     ["[Server thread/ERROR]: Encountered an unexpected exception", "This crash report has been saved to: x.txt"])
    asyncio.run(servers.check_crash("s"))
    s = state.get("s")
    assert started == ["s"] and s["phase"] == "starting" and "crash report" in s["phase_msg"]
    assert s["last_crash"]["reason"].startswith("crashed")
    assert any(e["action"] == "server.crash" and e["target"] == "s" for e in activity.read(10))


def test_oom_and_watchdog_reasons():
    assert "memory" in servers._crash_reason("s", {"code": 137, "oom": True}, [])
    assert "froze" in servers._crash_reason("s", {"code": 1, "oom": False}, ["A single server tick took 60.00 seconds"])
    assert "code 1" in servers._crash_reason("s", {"code": 1, "oom": False}, ["nothing useful"])


def test_crash_loop_gives_up(monkeypatch, tmp_path):
    import time
    now = time.time()
    started = _setup(monkeypatch, tmp_path, {"code": 1, "oom": False}, ["boom"], crashes=[now - 60, now - 30, now - 10])
    asyncio.run(servers.check_crash("s"))
    assert started == [] and state.get("s")["phase"] == "error" and "4 times" in state.get("s")["phase_msg"]


def test_auto_restart_off(monkeypatch, tmp_path):
    started = _setup(monkeypatch, tmp_path, {"code": 1, "oom": False}, ["boom"], auto_restart=False)
    asyncio.run(servers.check_crash("s"))
    assert started == [] and state.get("s")["phase"] == "error"


def test_ignored_while_something_else_runs(monkeypatch, tmp_path):
    started = _setup(monkeypatch, tmp_path, {"code": 1, "oom": False}, ["boom"])
    state.update("s", phase="stopping")
    asyncio.run(servers.check_crash("s"))
    assert started == []
