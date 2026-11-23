import asyncio
import os

import httpx
import pytest

from app import backups, dockerctl, servers, software, state


def _net(req):
    u = str(req.url)
    if "meta.fabricmc.net" in u:
        return httpx.Response(200, json=[{"loader": {"version": "0.19.5"}}] if u.endswith("/26.3") else [])
    if "fill.papermc.io" in u:
        if "/26.3/" in u:
            return httpx.Response(200, json=[{"id": 17, "channel": "BETA"}, {"id": 16, "channel": "ALPHA"}])
        if "/26.2/" in u:
            return httpx.Response(200, json=[{"id": 40, "channel": "STABLE"}])
        return httpx.Response(404, json={"message": "no such version"})
    if "api.purpurmc.org" in u:
        return httpx.Response(200, json={"builds": {"latest": "5", "all": ["5"]}}) if u.endswith("/26.3") else httpx.Response(404)
    return httpx.Response(500)


@pytest.fixture
def srv(tmp_path, monkeypatch):
    data = tmp_path / "data"
    d = data / "t"
    (d / "world").mkdir(parents=True)
    (d / "world" / "level.dat").write_text("w")
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(data / sid))
    monkeypatch.setattr(software, "_transport", httpx.MockTransport(_net))
    state.put({"id": "t", "name": "test", "kind": "vanilla", "loader": "vanilla", "mc_version": "26.3", "java_tag": "java25",
               "phase": "online", "memory": {"max_gb": 4, "init_gb": 2}, "imported": True, "rcon_password": "x"})
    calls = []
    run = {"on": True}
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running" if run["on"] else "exited")

    async def stop(sid):
        calls.append("stop"); run["on"] = False

    async def start(sid):
        calls.append("start"); run["on"] = True

    async def create(sid, reason="manual"):
        calls.append(f"backup:{reason}")
        return {"id": "snap1"}

    async def restore(sid, name):
        calls.append(f"restore:{name}")
    monkeypatch.setattr(servers, "stop", stop)
    monkeypatch.setattr(servers, "start", start)
    monkeypatch.setattr(backups, "create", create)
    monkeypatch.setattr(backups, "restore", restore)
    return d, calls


def test_choices():
    base = {"mc_version": "26.3"}
    assert software.choices({**base, "kind": "vanilla"}) == ["fabric", "paper", "purpur"]
    assert software.choices({**base, "kind": "fabric", "loader": "fabric"}) == ["vanilla"]
    assert software.choices({**base, "kind": "paper"}) == ["purpur"]
    assert software.choices({**base, "kind": "curseforge", "loader": "neoforge"}) == []      # modpacks: no
    assert software.choices({"kind": "vanilla", "mc_version": "LATEST"}) == []


def test_checks(srv):
    s = state.get("t")
    run = lambda t, mc="26.3": asyncio.run(software.check({**s, "mc_version": mc}, t))
    assert run("fabric")["ok"] and not run("fabric", "9.9")["ok"]
    p = run("paper")
    assert p["ok"] and p["channel"] == "experimental" and "beta" in p["note"]
    assert run("paper", "26.2") == {"ok": True, "note": "", "channel": None}
    assert not run("paper", "9.9")["ok"]
    assert run("purpur")["ok"] and not run("purpur", "9.9")["ok"]


def test_check_never_blocks_on_a_network_problem(srv, monkeypatch):
    def down(req):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(software, "_transport", httpx.MockTransport(down))
    c = asyncio.run(software.check(state.get("t"), "fabric"))
    assert c["ok"] and "Couldn't check" in c["note"]


def test_switch_to_paper_then_undo(srv):
    d, calls = srv

    async def go():
        await software.switch("t", "paper")
        await software._tasks["t"]
    asyncio.run(go())
    s = state.get("t")
    assert calls == ["backup:before-paper", "stop", "start"]
    assert s["kind"] == s["loader"] == "paper" and s["paper_channel"] == "experimental"
    assert s["software"]["step"] == "done" and s["software_prev"]["kind"] == "vanilla" and s["software_prev"]["backup"] == "snap1"
    env = dockerctl.build_env(s, 4)
    assert env["TYPE"] == "PAPER" and env["PAPER_CHANNEL"] == "experimental"
    assert software.choices(s) == ["purpur"]

    (d / "paper-26.3-17.jar").write_bytes(b"p")
    calls.clear()
    r = asyncio.run(software.undo("t"))
    s = state.get("t")
    assert r["restarted"] and calls == ["stop", "restore:snap1", "start"]
    assert s["kind"] == "vanilla" and not s.get("paper_channel") and not s.get("software_prev")
    assert not (d / "paper-26.3-17.jar").exists()
    assert "PAPER_CHANNEL" not in dockerctl.build_env(s, 4)


def test_fabric_to_vanilla_clears_fabric_so_it_isnt_detected_again(srv):
    d, calls = srv
    state.update("t", kind="fabric", loader="fabric", loader_version="0.19.5",
                 managed_mods=[{"slug": "lithium", "version_id": "x"}])
    (d / "fabric-server-mc.26.3-loader.0.19.5-launcher.1.1.1.jar").write_bytes(b"f")
    (d / ".fabric" / "server").mkdir(parents=True)
    (d / "libraries" / "net" / "fabricmc" / "fabric-loader" / "0.19.5").mkdir(parents=True)
    (d / "mods").mkdir()
    (d / "mods" / "lithium.jar").write_bytes(b"l")

    async def go():
        await software.switch("t", "vanilla")
        await software._tasks["t"]
    asyncio.run(go())
    s = state.get("t")
    assert s["kind"] == "vanilla" and s["loader_version"] is None and s["managed_mods"] == []
    assert s["software_prev"]["managed_mods"] == [{"slug": "lithium", "version_id": "x"}]
    assert not list(d.glob("fabric-server*.jar")) and not (d / ".fabric").exists()
    assert (d / "mods" / "lithium.jar").exists()                                 # mods are kept
    from app import importer
    assert importer.detect(str(d))["kind"] not in ("fabric", "quilt")          # nothing says Fabric any more
    assert dockerctl.build_env(s, 4)["TYPE"] == "VANILLA"


def test_refused_when_not_available_or_not_offered(srv):
    state.update("t", mc_version="9.9")
    with pytest.raises(ValueError, match="Fabric doesn't support"):
        asyncio.run(software.switch("t", "fabric"))
    with pytest.raises(ValueError, match="can't switch"):
        asyncio.run(software.switch("t", "forge"))
    assert not state.get("t").get("software")


def test_purpur_isnt_turned_back_into_paper_at_start(srv, monkeypatch):
    d, _ = srv
    state.update("t", kind="purpur", loader="purpur")
    (d / "config").mkdir()
    (d / "config" / "paper-global.yml").write_text("x")                          # Purpur keeps Paper's configs
    monkeypatch.setattr(servers, "pack_mod_ids", lambda s: set())
    s = asyncio.run(servers.reconcile_install(state.get("t")))
    assert s["loader"] == "purpur"
