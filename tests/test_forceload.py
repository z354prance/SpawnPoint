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
