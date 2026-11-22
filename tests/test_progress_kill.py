import asyncio
import os

from app import dockerctl, servers, state, topology


def _setup(monkeypatch, tmp_path, srv):
    monkeypatch.setattr(state, "_state", {"servers": {srv["id"]: srv}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))


def test_install_progress_steps(tmp_path, monkeypatch):
    s = {"id": "atm", "name": "atm", "phase": "installing", "loader": "neoforge", "pack": {"title": "ATM"},
         "pregen": {"spawn_radius": 750, "percent": 0}}
    _setup(monkeypatch, tmp_path, s)
    os.makedirs(tmp_path / "atm" / "mods")
    for i in range(3):
        (tmp_path / "atm" / "mods" / f"m{i}.jar").write_bytes(b"x" * 2**20)
    h = dockerctl.reset_hub("atm")
    g = servers.install_progress(s)
    assert (g["step"], g["percent"]) == (1, None) and "3 mods (3 MB)" in g["detail"]
    h._push("[init] Starting the Minecraft server...")
    assert servers.install_progress(s)["step"] == 2
    s.update(phase="preparing")
    assert servers.install_progress(s)["step"] == 3
    s.update(phase="pregen-spawn", pregen={"spawn_radius": 750, "percent": 62.0})
    g = servers.install_progress(s)
    assert (g["step"], g["percent"]) == (4, 62.0)
    s.update(phase="online")
    assert servers.install_progress(s) is None


def test_force_stop(tmp_path, monkeypatch):
    s = {"id": "x", "name": "x", "phase": "installing", "pregen": {"stage": "pending"}}
    _setup(monkeypatch, tmp_path, s)
    killed = []

    class C:
        def kill(self): killed.append(1)
    monkeypatch.setattr(dockerctl, "get_container", lambda sid: C())
    monkeypatch.setattr(dockerctl, "addr", lambda sid: "127.0.0.1")
    topology._allocator = topology.CoreAllocator([[0, 1], [2, 3]], 0)
    topology.allocator().assign_home("x", 1)

    async def run():
        never = asyncio.get_running_loop().create_future()
        servers._tasks["x"] = asyncio.ensure_future(never)  # stuck install task
        await servers.kill("x")
        await asyncio.sleep(0)
        return servers._tasks["x"].cancelled()
    assert asyncio.run(run()) is True
    assert killed == [1] and state.get("x")["phase"] == "stopped"
    assert topology.allocator().cpus_for("x") == []


def test_invite_only_writes_properties(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    s = {"id": "w", "name": "w", "phase": "stopped"}
    _setup(monkeypatch, tmp_path, s)
    os.makedirs(tmp_path / "w")
    (tmp_path / "w" / "server.properties").write_text("motd=hi\nwhite-list=false\n")
    monkeypatch.setattr(dockerctl, "status", lambda sid: "exited")
    main.app.dependency_overrides[main.require_auth] = lambda: {"name": "admin", "role": "admin"}
    c = TestClient(main.app)
    assert c.post("/api/servers/w/invite-only", json={"on": True}).json() == {"on": True, "applied_now": False}
    props = dict(servers.read_properties("w"))
    assert props["white-list"] == "true" and props["enforce-whitelist"] == "true"
    assert c.get("/api/servers/w/lists").json()["invite_only"] is True
    main.app.dependency_overrides.clear()
