import asyncio
import hashlib
import os

import httpx
import pytest

from app import backups, dockerctl, servers, state, upgrades


@pytest.fixture
def srv(tmp_path, monkeypatch):
    data = tmp_path / "data"
    d = data / "u"
    (d / "mods").mkdir(parents=True)
    (d / "world").mkdir()
    (d / "world" / "level.dat").write_text("w")
    (d / "mods" / "lithium-0.14.jar").write_bytes(b"lith-old")
    (d / "mods" / "oldthing-1.0.jar").write_bytes(b"old-thing")
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(data / sid))
    monkeypatch.setattr(upgrades.config, "DATA_DIR", str(data))
    state.put({"id": "u", "name": "U", "kind": "fabric", "loader": "fabric", "mc_version": "1.21.1", "java_tag": "java21",
               "phase": "online", "memory": {"max_gb": 4, "init_gb": 2}, "pregen": {"stage": "done"}, "loader_version": "0.15.0"})
    lith, thing, new = hashlib.sha1(b"lith-old").hexdigest(), hashlib.sha1(b"old-thing").hexdigest(), b"lith-new"

    def handler(req):
        import json
        if req.url.path == "/v2/version_files":
            return httpx.Response(200, json={lith: {"id": "l1", "project_id": "L", "version_number": "0.14"},
                                             thing: {"id": "t1", "project_id": "T", "version_number": "1.0"}})
        if req.url.path == "/v2/version_files/update":
            assert json.loads(req.content)["game_versions"] == ["1.21.4"]
            return httpx.Response(200, json={lith: {"id": "l2", "project_id": "L", "version_number": "0.15", "files": [
                {"primary": True, "filename": "lithium-0.15.jar", "url": "https://cdn.modrinth.com/l.jar",
                 "hashes": {"sha1": hashlib.sha1(new).hexdigest()}}]}})
        if req.url.path == "/v2/projects":
            return httpx.Response(200, json=[{"id": "L", "title": "Lithium"}, {"id": "T", "title": "Old Thing"}])
        if req.url.host == "cdn.modrinth.com":
            return httpx.Response(200, content=new)
        return httpx.Response(404)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def create(sid, label):
        return {"id": "snap1"}
    monkeypatch.setattr(backups, "create", create)
    calls = []

    async def stop(sid):
        calls.append("stop")

    async def start(sid):
        calls.append("start")

    async def arcmd(s, cmd):
        calls.append(cmd)
        return ""
    monkeypatch.setattr(servers, "stop", stop)
    monkeypatch.setattr(servers, "start", start)
    monkeypatch.setattr(servers, "arcmd", arcmd)
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running")
    return d, calls


def _wait(sid):
    async def go():
        await upgrades.start(sid, "1.21.4")
        await upgrades._tasks[sid]
    asyncio.run(go())


def test_plan(srv):
    p = asyncio.run(upgrades.plan("u", "1.21.4"))
    assert p["java"] == "java21" and p["missing"] == 1 and p["missing_off"]
    assert {m["title"]: m["status"] for m in p["mods"]} == {"Lithium": "update", "Old Thing": "none"}
    with pytest.raises(ValueError):
        asyncio.run(upgrades.plan("u", "1.20.1"))


def test_trial_passes_then_switches(srv, monkeypatch):
    d, calls = srv
    seen = {}

    def trial(s, trial_dir, target):
        seen["mods"] = sorted(os.listdir(os.path.join(trial_dir, "mods")))
        seen["real"] = sorted(os.listdir(d / "mods"))
        return True, ["Done (5s)!"]
    monkeypatch.setattr(upgrades, "_trial_boot", trial)
    _wait("u")
    assert seen["mods"] == ["lithium-0.15.jar", "oldthing-1.0.jar.disabled"]
    assert seen["real"] == ["lithium-0.14.jar", "oldthing-1.0.jar"]          # untouched during the trial
    s = state.get("u")
    assert s["mc_version"] == "1.21.4" and s["loader_version"] is None and s["upgrade"]["step"] == "done"
    assert sorted(os.listdir(d / "mods")) == ["lithium-0.15.jar", "oldthing-1.0.jar.disabled"]
    assert calls[:3] == ["save-off", "save-all flush", "save-on"] and calls[-2:] == ["stop", "start"]
    assert s["upgrade_prev"]["mc_version"] == "1.21.1" and s["upgrade_prev"]["backup"] == "snap1"
    assert not os.path.exists(upgrades._trial_dir("u"))


def test_trial_fails_and_nothing_changes(srv, monkeypatch):
    d, calls = srv
    monkeypatch.setattr(upgrades, "_trial_boot", lambda s, t, v: (False, [
        "Mod ID: 'fabric-api', Requested by: 'lithium', Expected range: '*', Actual version: '[MISSING]'"]))
    _wait("u")
    s = state.get("u")
    assert s["mc_version"] == "1.21.1" and s["upgrade"]["step"] == "failed"
    assert s["upgrade"]["result"]["explain"]["title"] == "A mod is missing something it needs"
    assert sorted(os.listdir(d / "mods")) == ["lithium-0.14.jar", "oldthing-1.0.jar"]
    assert "stop" not in calls and not os.path.exists(upgrades._trial_dir("u"))
