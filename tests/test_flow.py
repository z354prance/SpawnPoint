"""Simulated end-to-end: create -> install -> prepare -> spawn pregen -> online -> background pregen -> done,
with Docker, RCON and Modrinth faked."""
import asyncio
import os

from app import catalog, config, dockerctl, perf, pregen, router, servers, state, topology


class FakeContainer:
    status = "running"
    def stop(self, timeout=0): self.status = "exited"


def test_full_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    monkeypatch.setattr(pregen, "POLL_S", 0.05)
    monkeypatch.setattr(pregen, "HOLD_S", 0)
    monkeypatch.setattr(topology, "_allocator",
                        topology.CoreAllocator([[2*i, 2*i+1, 2*i+24, 2*i+25] for i in range(12)], 2))
    events, cpusets, routes = [], [], {}
    box = {"c": FakeContainer()}

    def replace_container(s, cpus, workers, start=True):
        events.append(("boot", len(cpus), s["first_boot"]))
        box["c"] = FakeContainer()
        os.makedirs(tmp_path / s["id"] / "plugins", exist_ok=True)
        # emulate log output a moment after boot
        threading_push(s["id"], "[Server thread/INFO]: Done (3.2s)! For help, type \"help\"")
    import threading
    def threading_push(sid, line):
        threading.Timer(0.05, lambda: dockerctl.hub(sid)._push(line)).start()

    monkeypatch.setattr(dockerctl, "replace_container", replace_container)
    monkeypatch.setattr(dockerctl.LogHub, "ensure_running", lambda self: None)
    monkeypatch.setattr(dockerctl, "get_container", lambda sid: box["c"])
    monkeypatch.setattr(dockerctl, "status", lambda sid: box["c"].status)
    monkeypatch.setattr(dockerctl, "set_cpuset", lambda sid, cpus: cpusets.append(len(cpus)))
    monkeypatch.setattr(dockerctl, "pin_threads", lambda sid, cpus: {"pinned": True, "workers": 3})

    mspt = {"seq": [55.0, 55.0]}   # a sustained spike, then normal
    def fake_rcmd(s, cmd):
        events.append(("rcon", cmd))
        if cmd == "chunky start":
            threading_push(s["id"], "[Chunky] Task running for minecraft:overworld. Processed: 10 chunks (42.00%)")
            threading.Timer(1.5, lambda: dockerctl.hub(s["id"])._push("[Chunky] Task finished for minecraft:overworld.")).start()
        if cmd == "tick query":
            v = mspt["seq"].pop(0) if mspt["seq"] else 20.0
            return f"Average time per tick: {v}ms (Target: 50.0ms)"
        return "There are 0 of a max of 20 players online:" if cmd == "list" else "ok"
    monkeypatch.setattr(servers, "rcmd", fake_rcmd)

    async def fake_download(slug, loader, ver, dest):
        os.makedirs(dest, exist_ok=True)
        open(os.path.join(dest, f"{slug}.jar"), "w").close()
        return {"filename": f"{slug}.jar", "client_side": "optional"}
    monkeypatch.setattr(catalog, "download_mod", fake_download)
    # paper-global.yml present after first boot
    os.makedirs(tmp_path / "survival" / "config", exist_ok=True)
    (tmp_path / "survival" / "config" / "paper-global.yml").write_text("chunk-system:\n  io-threads: -1\n")

    async def run():
        s = await servers.create({"kind": "paper", "mc_version": "1.21.1", "name": "survival"})
        for _ in range(200):
            await asyncio.sleep(0.05)
            if state.get(s["id"])["pregen"]["stage"] == "done":
                break
        return state.get(s["id"])

    s = asyncio.run(run())
    assert s["phase"] == "online", s.get("phase_msg")
    assert s["pregen"]["stage"] == "done"
    assert s["hostname"] == "survival.mc.example.com"
    boots = [e for e in events if e[0] == "boot"]
    assert boots[0] == ("boot", 16, True)         # install borrows spare cores too (faster first boot)
    assert boots[1] == ("boot", 16, False)        # pregen boot: own group + 3 borrowed
    assert cpusets[-1] == 4                       # borrowed cores returned
    cmds = [e[1] for e in events if e[0] == "rcon"]
    assert "chunky radius 750" in cmds and "chunky radius 5000" in cmds
    assert "chunky pause" in cmds and "chunky continue" in cmds   # throttled on high mspt
    assert s["helpers_added"] == ["chunky.jar", "spark.jar"]
    assert "worker-threads: 16" in (tmp_path / "survival/config/paper-global.yml").read_text()
    assert perf.java_tag(s["mc_version"]) == s["java_tag"]


def test_shutdown_and_resume(tmp_path, monkeypatch):
    """Isolated mode: shutdown stops running servers and flags them; next boot starts them again."""
    monkeypatch.setattr(config, "ISOLATED", True)
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {
        "a": {"id": "a", "name": "a", "phase": "online", "rcon_password": "x", "home_groups": 1, "pregen": {"stage": "done"}},
        "b": {"id": "b", "name": "b", "phase": "stopped", "rcon_password": "x", "home_groups": 1, "pregen": {"stage": "done"}},
    }})
    status = {"a": "running", "b": "exited"}
    stopped, started = [], []

    class C:
        def __init__(self, sid): self.sid = sid
        def stop(self, timeout=0): stopped.append(self.sid); status[self.sid] = "exited"
    monkeypatch.setattr(dockerctl, "status", lambda sid: status[sid])
    monkeypatch.setattr(dockerctl, "get_container", lambda sid: C(sid))
    monkeypatch.setattr(servers, "rcmd", lambda s, c: "ok")
    async def fake_start(sid): started.append(sid)
    monkeypatch.setattr(servers, "start", fake_start)

    async def run():
        await servers.shutdown_all()
        assert state.get("a")["resume_on_boot"] is True
        await servers.resume_all()
    asyncio.run(run())
    assert stopped == ["a"] and started == ["a"]
    assert "resume_on_boot" not in state.get("a")


def test_pregen_doesnt_flap(monkeypatch):
    """One high reading doesn't pause; a pause holds for HOLD_S before resuming."""
    import asyncio
    from app import pregen, dockerctl
    monkeypatch.setattr(pregen, "POLL_S", 0.01)
    monkeypatch.setattr(pregen, "HOLD_S", 0.2)
    readings = iter([55, 20, 55, 55, 20, 20, 20, 20] + [20] * 400)
    cmds = []

    class Hub:
        seq = 0
        def since(self, n): return []
    monkeypatch.setattr(dockerctl, "hub", lambda sid: Hub())
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running")
    async def tick(s):
        v = float(next(readings)); return {"avg": v / 3, "p95": v}   # spikes show in p95, not the average
    async def chunky(s, *c): cmds.extend(c)
    async def players(s): return 0
    monkeypatch.setattr(pregen, "_tick", tick)
    monkeypatch.setattr(pregen, "_players", players)
    monkeypatch.setattr(pregen.state, "get", lambda sid: {})
    monkeypatch.setattr(pregen, "_chunky", chunky)
    monkeypatch.setattr(pregen, "_pg", lambda sid, **k: None)

    async def go():
        t = asyncio.create_task(pregen._wait_task({"id": "x"}, 0, throttle=True))
        await asyncio.sleep(0.6); t.cancel()
    asyncio.run(go())
    assert cmds == ["pause", "continue"]


def test_startup_cores_given_back(monkeypatch):
    """Cores borrowed to start go back after settling, unless pre-gen is using them."""
    import asyncio
    from app import servers, topology, dockerctl, state

    class A:
        def __init__(self): self.burst = {"a": [5], "b": [6]}
        def return_burst(self, sid): self.burst.pop(sid, None)
        def cpus_for(self, sid): return [0, 1]
    alloc = A()
    monkeypatch.setattr(topology, "allocator", lambda: alloc)
    monkeypatch.setattr(servers, "BOOST_SETTLE_S", 0)
    sets = []
    monkeypatch.setattr(dockerctl, "set_cpuset", lambda sid, cpus: sets.append(sid))
    db = {"a": {"pregen": {"stage": "done"}}, "b": {"pregen": {"stage": "background"}}}
    monkeypatch.setattr(state, "get", lambda sid: db.get(sid))

    async def go():
        await servers._end_boost("a"); await servers._end_boost("b")
    asyncio.run(go())
    assert "a" not in alloc.burst and sets == ["a"]
    assert alloc.burst["b"] == [6]


def test_pregen_can_wait_while_players_are_online(monkeypatch):
    import asyncio
    from app import pregen, dockerctl
    monkeypatch.setattr(pregen, "POLL_S", 0.01)
    monkeypatch.setattr(pregen, "HOLD_S", 0.05)
    online = iter([1] * 5 + [0] * 1000)
    cmds = []

    class Hub:
        seq = 0
        def since(self, n): return []
    monkeypatch.setattr(dockerctl, "hub", lambda sid: Hub())
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running")
    async def tick(s): return {"avg": 3.0, "p95": 5.0}         # no lag at all
    async def players(s): return next(online)
    async def chunky(s, *c): cmds.extend(c)
    monkeypatch.setattr(pregen, "_tick", tick)
    monkeypatch.setattr(pregen, "_players", players)
    monkeypatch.setattr(pregen, "_chunky", chunky)
    monkeypatch.setattr(pregen, "_pg", lambda sid, **k: None)
    monkeypatch.setattr(pregen.state, "get", lambda sid: {"pregen_pause_when_online": True})   # the opt-in setting

    async def go():
        t = asyncio.create_task(pregen._wait_task({"id": "x"}, 0, throttle=True))
        await asyncio.sleep(0.4); t.cancel()
    asyncio.run(go())
    assert cmds == ["pause", "continue"]          # paused for the player, resumed once they'd left


def test_tick_percentiles():
    from app import rcon
    out = "Average time per tick: 4.2ms (Target: 50.0ms)\nPercentiles: P50: 3.1ms P95: 61.0ms P99: 90.2ms, sample: 100"
    assert rcon.parse_tick(out) == {"avg": 4.2, "p95": 61.0}
    assert rcon.parse_tick("Tick durations (min/med/95%ile/max ms) from last 10s, 1m:\n 1.0/3.0/48.5/120.0; 1/2/3/4") == {"avg": 3.0, "p95": 48.5}


def test_pregen_keeps_going_with_players_by_default(monkeypatch):
    import asyncio
    from app import pregen, dockerctl
    monkeypatch.setattr(pregen, "POLL_S", 0.01)
    cmds = []

    class Hub:
        seq = 0
        def since(self, n): return []
    monkeypatch.setattr(dockerctl, "hub", lambda sid: Hub())
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running")
    async def tick(s): return {"avg": 3.0, "p95": 6.0}
    async def players(s): return 3
    async def chunky(s, *c): cmds.extend(c)
    monkeypatch.setattr(pregen, "_tick", tick); monkeypatch.setattr(pregen, "_players", players)
    monkeypatch.setattr(pregen, "_chunky", chunky); monkeypatch.setattr(pregen, "_pg", lambda sid, **k: None)
    monkeypatch.setattr(pregen.state, "get", lambda sid: {})

    async def go():
        t = asyncio.create_task(pregen._wait_task({"id": "x"}, 0, throttle=True))
        await asyncio.sleep(0.2); t.cancel()
    asyncio.run(go())
    assert cmds == []                               # players online, no lag: it keeps generating


def test_create_plain_server_with_seed(monkeypatch, tmp_path):
    """Regression: creating a vanilla/Paper server returned a 500 after it was created."""
    from fastapi.testclient import TestClient
    from app import config, dockerctl, main, servers, state, users
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json")); monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "servers"))
    monkeypatch.setattr(servers, "spawn", lambda sid, coro: coro.close())
    monkeypatch.setattr(dockerctl, "status", lambda sid: "absent")
    monkeypatch.setattr(dockerctl, "mem_usage_bytes", lambda sid: 0)
    users._fails.clear(); users.ensure_bootstrap()
    c = TestClient(main.app)
    assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
    r = c.post("/api/servers", json={"kind": "vanilla", "mc_version": "1.21.4", "name": "plain", "seed": " 8675309 "})
    assert r.status_code == 200, r.text
    s = state.get(r.json()["id"])
    assert s["seed"] == "8675309"
    env = dockerctl.build_env(s, 4)
    assert env["SEED"] == "8675309"
    assert "SEED" not in dockerctl.build_env(dict(s, first_boot=False), 4)      # only for the brand-new world
    assert c.post("/api/servers", json={"kind": "paper", "mc_version": "1.21.4", "name": "p2", "seed": "x" * 80}).status_code == 422


def test_modded_installs_without_a_throwaway_start(tmp_path, monkeypatch):
    """Fabric/NeoForge/modpacks: install with SETUP_ONLY (no game start), tune, then one real start."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    monkeypatch.setattr(pregen, "POLL_S", 0.05)
    monkeypatch.setattr(pregen, "HOLD_S", 0)
    monkeypatch.setattr(topology, "_allocator",
                        topology.CoreAllocator([[2*i, 2*i+1, 2*i+24, 2*i+25] for i in range(12)], 2))
    import threading
    boots, box = [], {"c": FakeContainer()}

    def replace_container(s, cpus, workers, start=True):
        env = dockerctl.build_env(s, workers)
        boots.append(("setup" if env.get("SETUP_ONLY") == "true" else "game", s["first_boot"], s["memory"]["max_gb"]))
        box["c"] = FakeContainer()
        os.makedirs(tmp_path / s["id"] / "mods", exist_ok=True)
        if env.get("SETUP_ONLY") == "true":
            for i in range(160):                            # the pack's mods land during setup
                open(tmp_path / s["id"] / "mods" / f"m{i}.jar", "w").close()
            box["c"].status = "exited"
        else:
            threading.Timer(0.05, lambda: dockerctl.hub(s["id"])._push('[Server thread/INFO]: Done (3.2s)! For help, type "help"')).start()
    monkeypatch.setattr(dockerctl, "replace_container", replace_container)
    monkeypatch.setattr(dockerctl, "exit_info", lambda sid: {"code": 0, "oom": False})
    monkeypatch.setattr(dockerctl.LogHub, "ensure_running", lambda self: None)
    monkeypatch.setattr(dockerctl, "get_container", lambda sid: box["c"])
    monkeypatch.setattr(dockerctl, "status", lambda sid: box["c"].status)
    monkeypatch.setattr(dockerctl, "set_cpuset", lambda sid, cpus: None)
    monkeypatch.setattr(dockerctl, "pin_threads", lambda sid, cpus: {"pinned": True, "workers": 3})

    def fake_rcmd(s, cmd):
        if cmd == "chunky start":
            threading.Timer(0.3, lambda: dockerctl.hub(s["id"])._push("[Chunky] Task finished for minecraft:overworld.")).start()
        if cmd == "tick query":
            return "Average time per tick: 20.0ms (Target: 50.0ms)"
        return "There are 0 of a max of 20 players online:" if cmd == "list" else "ok"
    monkeypatch.setattr(servers, "rcmd", fake_rcmd)

    async def fake_download(slug, loader, ver, dest):
        os.makedirs(dest, exist_ok=True)
        open(os.path.join(dest, f"{slug}.jar"), "w").close()
        return {"filename": f"{slug}.jar", "client_side": "optional"}
    monkeypatch.setattr(catalog, "download_mod", fake_download)

    async def run():
        s = await servers.create({"kind": "fabric", "mc_version": "1.21.1", "name": "modded"})
        seen = set()
        for _ in range(300):
            await asyncio.sleep(0.05)
            p = servers.install_progress(state.get(s["id"]))
            if p:
                seen.add(p["label"])
            if state.get(s["id"])["pregen"]["stage"] == "done":
                break
        return state.get(s["id"]), seen

    s, seen = asyncio.run(run())
    assert s["phase"] == "online", s.get("phase_msg")
    assert [b[0] for b in boots][:2] == ["setup", "game"]           # one real start, not two
    assert boots[0][1] is True and boots[1][1] is False
    assert boots[1][2] == perf.memory_plan("fabric", s["mod_count"])[2]   # sized from the real mod count before starting
    assert "chunky.jar" in s["helpers_added"]
    assert {"Downloading", "Generating spawn"} <= seen


def test_rcon_is_put_back_on(tmp_path, monkeypatch):
    """A pack's server.properties can switch RCON off; SpawnPoint turns it back on before starting."""
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    state.put({"id": "r", "name": "r", "rcon_password": "pw123", "pregen": {"stage": "done"}})
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "server.properties").write_text("motd=x\nenable-rcon=false\nrcon.password=\n")
    assert servers.ensure_rcon(state.get("r")) is True
    props = dict(servers.read_properties("r"))
    assert props["enable-rcon"] == "true" and props["rcon.password"] == "pw123" and props["rcon.port"] == "25575"
    assert props["motd"] == "x"
    assert servers.ensure_rcon(state.get("r")) is False
    env = dockerctl.build_env({**state.get("r"), "java_tag": "java21", "memory": {"init_gb": 1, "max_gb": 2},
                               "kind": "fabric", "mc_version": "1.21.1", "props_from_env": True}, 2)
    assert env["SKIP_SERVER_PROPERTIES"] == "false" and env["RCON_PASSWORD"] == "pw123"
