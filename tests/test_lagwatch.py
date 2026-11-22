import asyncio
import time

from app import activity, alerts, dockerctl, lagwatch, servers, state


def _setup(monkeypatch, tmp_path, spark_reply="Started profiler", log_line="[⚡] Profiler stopped & uploaded. View the results here: https://spark.lucko.me/AbC123x"):
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    state.put({"id": "a", "name": "aoc", "phase": "online"})
    lagwatch._recent.clear(); lagwatch._running.clear()
    sent, cmds = [], []
    monkeypatch.setattr(alerts, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running")

    class Hub:
        seq = 0
        def since(self, n): return [(1, log_line)] if n < 1 else []
    monkeypatch.setattr(dockerctl, "hub", lambda sid: Hub())

    async def arcmd(s, cmd):
        cmds.append(cmd); return spark_reply
    monkeypatch.setattr(servers, "arcmd", arcmd)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    return sent, cmds


_real_sleep = asyncio.sleep
async def _fast_sleep(s): await _real_sleep(0)


def _feed(values):
    async def go():
        for v in values:
            lagwatch.note("a", v, 6.0, 4)
        for _ in range(20):
            await _real_sleep(0.01)
    asyncio.run(go())


def test_sustained_lag_records_a_report(monkeypatch, tmp_path):
    sent, cmds = _setup(monkeypatch, tmp_path)
    _feed([90.0] * 11 + [10.0] * 7)                       # 11 of 18: not enough
    assert cmds == []
    _feed([90.0] * 12)                                    # most of 3 minutes over the line
    assert cmds == ["spark profiler start --timeout 60"]
    reps = state.get("a")["lag_reports"]
    assert reps[0]["url"] == "https://spark.lucko.me/AbC123x" and reps[0]["p95"] == 90.0 and reps[0]["players"] == 4
    assert sent and sent[0][0] == "lag_report"
    assert any(e["action"] == "server.lag" for e in activity.read(10))
    _feed([90.0] * 18)                                    # cooldown: no second report straight away
    assert len(cmds) == 1


def test_no_spark_and_turned_off(monkeypatch, tmp_path):
    sent, cmds = _setup(monkeypatch, tmp_path, spark_reply="Unknown or incomplete command")
    _feed([90.0] * 18)
    assert state.get("a").get("lag_no_spark") and "lag_reports" not in state.get("a")
    state.update("a", lag_no_spark=None, lag_tried=0, lag_reports=False)
    cmds.clear()
    _feed([90.0] * 18)
    assert cmds == []                                     # switched off in Stats
