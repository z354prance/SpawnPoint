import json
"""Pre-generation without Chunky: /forceload batches, checked against the region files."""
import asyncio
import os

from app import config, dockerctl, mapper, pregen, rcon, servers, state
from tests.test_mapper import chunk_118, write_region


def _setup(monkeypatch, tmp_path, radius_blocks=200):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running")
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(mapper, "spawn_point", lambda sid: (0, 0))
    monkeypatch.setattr(pregen, "CHECK_S", 0.01)
    os.makedirs(tmp_path / "v" / "world" / "region")
    state.put({"id": "v", "name": "v", "loader": "vanilla", "pregen": {"stage": "spawn", "spawn_radius": radius_blocks,
                                                                     "full_radius": 400, "percent": 0}})
    loaded: list = []
    cmds: list = []

    async def arcmd(s, cmd):
        cmds.append(cmd)
        if cmd.startswith("forceload add"):
            loaded.append(tuple(int(v) // 16 for v in cmd.split()[2:]))
        if cmd == "save-all":                                  # the game writes the loaded chunks as finished
            regions: dict = {}
            for x1, z1, x2, z2 in loaded:
                for x in range(x1, x2 + 1):
                    for z in range(z1, z2 + 1):
                        regions.setdefault((x >> 5, z >> 5), {})[(x & 31, z & 31)] = chunk_118(70)
            for (rx, rz), chunks in regions.items():
                path = tmp_path / "v" / "world" / "region" / f"r.{rx}.{rz}.mca"
                write_region(str(path), chunks)
        if cmd == "forceload query":
            return "No chunks were found to be forceloaded"
        return ""
    monkeypatch.setattr(servers, "arcmd", arcmd)
    return cmds


def test_blocks_cover_the_square_nearest_first():
    b = pregen._blocks(0, 0, 200)                               # 13 chunks each way
    covered = {(x, z) for x1, z1, x2, z2 in b for x in range(x1, x2 + 1) for z in range(z1, z2 + 1)}
    assert covered == {(x, z) for x in range(-13, 14) for z in range(-13, 14)}
    assert all(x2 - x1 < pregen.BLOCK and z2 - z1 < pregen.BLOCK for x1, z1, x2, z2 in b)
    first = b[0]
    assert first[0] <= 0 <= first[2] or abs(first[0]) <= pregen.BLOCK      # starts at spawn


def test_forceload_generates_and_resumes(monkeypatch, tmp_path):
    cmds = _setup(monkeypatch, tmp_path)
    s = state.get("v")
    assert asyncio.run(pregen.forceload_supported(s))
    assert asyncio.run(pregen.forceload_gen(s, 100, throttle=False)) is True
    adds = [c for c in cmds if c.startswith("forceload add")]
    removes = [c for c in cmds if c.startswith("forceload remove")]
    assert adds and len(adds) == len(removes) and state.get("v")["pregen"]["percent"] == 100
    assert all(pregen._missing("v", b) == 0 for b in pregen._blocks(0, 0, 100))
    cmds.clear()                                                 # running again skips what's on disk
    assert asyncio.run(pregen.forceload_gen(state.get("v"), 100, throttle=False)) is True
    assert not [c for c in cmds if c.startswith("forceload add")]


def test_no_chunky_no_forceload_is_skipped(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    async def old(s, cmd):
        return "Unknown command"                                 # 1.12-era server: neither exists
    monkeypatch.setattr(servers, "arcmd", old)
    assert asyncio.run(pregen._generate(state.get("v"), 200, throttle=False, fresh_task=True)) is None


def test_pause_button_is_remembered(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    async def tick(s): return {"avg": 2.0, "p95": 3.0}
    async def players(s): return 0
    monkeypatch.setattr(pregen, "_tick", tick); monkeypatch.setattr(pregen, "_players", players)
    monkeypatch.setattr(pregen, "HOLD_S", 0)
    pregen._pg("v", held=True)
    th = pregen.Throttle("v")
    assert asyncio.run(th.step(state.get("v"))) == "pause"
    for _ in range(3):
        assert asyncio.run(th.step(state.get("v"))) is None      # no lag, but it stays paused
    pregen._pg("v", held=False)
    assert asyncio.run(th.step(state.get("v"))) == "resume"  # released: carries on (nothing else is wrong)


def test_several_batches_load_at_once(monkeypatch, tmp_path):
    cmds = _setup(monkeypatch, tmp_path)
    asyncio.run(pregen.forceload_gen(state.get("v"), 300, throttle=False))
    most, now = 0, 0
    for c in cmds:
        now += c.startswith("forceload add") - c.startswith("forceload remove")
        most = max(most, now)
    assert most == pregen.IN_FLIGHT and now == 0


def test_chunky_replaces_a_task_it_did_not_start(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    sent = []

    async def arcmd(s, cmd):
        sent.append(cmd)
        if cmd == "chunky cancel minecraft:overworld":
            return "[Chunky] Cancelled tasks can't be continued. Type '/chunky confirm' to cancel."
        if cmd == "chunky start":
            return "[Chunky] Found a paused task. Use '/chunky continue' to continue it or '/chunky confirm' to start over."
        return "[Chunky] ok"
    monkeypatch.setattr(servers, "arcmd", arcmd)
    async def wait(s, after, throttle): return True
    monkeypatch.setattr(pregen, "_wait_task", wait)
    state.update("v", imported=True)
    pregen._pg("v", percent=12.3, method="chunky")              # the old host's task, running when imported
    assert asyncio.run(pregen._generate(state.get("v"), 400, throttle=True, fresh_task=False))
    assert "chunky continue" not in sent
    assert sent.count("chunky confirm") == 2 and "chunky radius 400" in sent
    assert state.get("v")["pregen"]["chunky_task"] == 400
    sent.clear()                                                # now it's ours: carry on with it
    asyncio.run(pregen._generate(state.get("v"), 400, throttle=True, fresh_task=False))
    assert sent == ["chunky continue"]


def test_empty_pause_off_while_generating(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    p = tmp_path / "v" / "server.properties"
    p.write_text("motd=v\npause-when-empty-seconds=60\n")
    servers.apply_empty_pause(state.get("v"))
    assert "pause-when-empty-seconds=0" in p.read_text() and state.get("v")["empty_pause_restore"] == "60"
    pregen._pg("v", stage="done")
    servers.apply_empty_pause(state.get("v"))
    assert "pause-when-empty-seconds=60" in p.read_text() and state.get("v")["empty_pause_restore"] is None
    p.write_text("motd=v\n")                                    # older versions don't have it: left alone
    pregen._pg("v", stage="background")
    servers.apply_empty_pause(state.get("v"))
    assert "pause-when-empty" not in p.read_text()


def test_watchdog_and_chunky_while_generating(monkeypatch, tmp_path):
    """No watchdog crash loop while pre-generating: a longer max-tick-time, and Chunky
    doesn't resume by itself at full speed after a restart."""
    _setup(monkeypatch, tmp_path)
    p = tmp_path / "v" / "server.properties"
    p.write_text("motd=v\nmax-tick-time=60000\n")
    cj = tmp_path / "v" / "config" / "chunky" / "config.json"
    cj.parent.mkdir(parents=True)
    cj.write_text('{"version": 2, "continueOnRestart": true, "silent": false}')
    pregen._pg("v", stage="background")
    servers.apply_empty_pause(state.get("v"))
    assert "max-tick-time=300000" in p.read_text() and state.get("v")["tick_time_restore"] == "60000"
    assert json.loads(cj.read_text())["continueOnRestart"] is False
    pregen._pg("v", stage="done")
    servers.apply_empty_pause(state.get("v"))
    assert "max-tick-time=60000" in p.read_text() and state.get("v")["tick_time_restore"] is None
    p.write_text("motd=v\nmax-tick-time=-1\n")                # watchdog already off: left alone
    pregen._pg("v", stage="background")
    servers.apply_empty_pause(state.get("v"))
    assert "max-tick-time=-1" in p.read_text()


def test_skipped_pregen_comes_back_with_chunky(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    pregen._pg("v", stage="skipped")
    monkeypatch.setattr(servers, "has_mod", lambda s, slug, *a: slug == "chunky")
    calls = []

    async def boot_ready(sid, timeout, burst=False):
        calls.append("boot")
        return True

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(servers, "boot_ready", boot_ready)
    monkeypatch.setattr(servers, "go_online", nothing)
    monkeypatch.setattr(pregen, "background", nothing)
    asyncio.run(pregen.run("v", fresh=False))
    assert state.get("v")["pregen"]["stage"] == "background" and state.get("v")["pregen"]["method"] == "chunky"


def test_forceload_finds_26_1_worlds(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    import shutil
    old = tmp_path / "v" / "world" / "region"
    new = tmp_path / "v" / "world" / "dimensions" / "minecraft" / "overworld" / "region"
    shutil.rmtree(old); new.mkdir(parents=True)
    os.symlink(new, old)                      # the fake game writes to world/region; here that's the new folder
    monkeypatch.setattr(mapper, "level_name", lambda sid: "world")
    assert mapper.region_dir("v") == str(new)
    assert asyncio.run(pregen.forceload_gen(state.get("v"), 60, throttle=False)) is True
    assert all(pregen._missing("v", b) == 0 for b in pregen._blocks(0, 0, 60))


def test_without_chunky_only_the_spawn_area(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    pregen._pg("v", stage="background", method="forceload", percent=3.0, paused=True, held=True)
    asyncio.run(pregen.background("v"))
    p = state.get("v")["pregen"]
    assert p["stage"] == "done" and p["spawn_only"] and not p["held"]
