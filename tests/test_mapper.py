import asyncio
import gzip
import io
import json
import os
import struct
import subprocess
import sys
import time
import zipfile
import zlib

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, dockerctl, flatmap, main, mapper, servers, state, topology, users


# ------------------------------------------------------------------ tiny NBT + region writers
def _s(t): b = t.encode(); return struct.pack(">H", len(b)) + b
def B(n, v): return b"\x01" + _s(n) + struct.pack(">b", v)
def I(n, v): return b"\x03" + _s(n) + struct.pack(">i", v)
def S(n, v): return b"\x08" + _s(n) + _s(v)
def L(n, arr): return b"\x0c" + _s(n) + struct.pack(">i", len(arr)) + b"".join(struct.pack(">Q", int(v)) for v in arr)
def C(n, *body): return b"\x0a" + _s(n) + b"".join(body) + b"\x00"
def LISTC(n, items): return b"\x09" + _s(n) + struct.pack(">bi", 10, len(items)) + b"".join(b"".join(i) + b"\x00" for i in items)


def pack(vals, bits, tight=False):
    if tight:
        big = 0
        for i, v in enumerate(vals):
            big |= int(v) << (i * bits)
        return [(big >> (64 * i)) & ((1 << 64) - 1) for i in range(-(-len(vals) * bits // 64))]
    per, out = 64 // bits, []
    for i in range(0, len(vals), per):
        v = 0
        for j, x in enumerate(vals[i:i + per]):
            v |= int(x) << (j * bits)
        out.append(v)
    return out


def chunk_118(top_y, top_block="minecraft:grass_block", floor=None, status="minecraft:full"):
    """1.18+ chunk: stone up to top_y-1, top_block at top_y (per column via a 16x16 array)."""
    min_y = -64
    top = np.full((16, 16), top_y) if np.isscalar(top_y) else np.asarray(top_y)
    pal = ["minecraft:air", "minecraft:stone", top_block]
    secs = []
    for sy in range(-4, 20):
        idx = np.zeros(4096, dtype=int)
        for y in range(16):
            wy = sy * 16 + y
            layer = np.where(wy < top, 1, np.where(wy == top, 2, 0))
            idx[y * 256:(y + 1) * 256] = layer.reshape(-1)
        if not idx.any():
            secs.append([B("Y", sy), C("block_states", LISTC("palette", [[S("Name", "minecraft:air")]]))])
            continue
        secs.append([B("Y", sy), C("block_states", LISTC("palette", [[S("Name", p)] for p in pal]), L("data", pack(idx, 4)))])
    hm = (top + 1 - min_y).reshape(-1)
    of = ((floor if floor is not None else top) + 1 - min_y).reshape(-1) if floor is not None else hm
    body = I("DataVersion", 3955) + S("Status", status) + LISTC("sections", secs) + \
        C("Heightmaps", L("MOTION_BLOCKING", pack(hm, 9)), L("OCEAN_FLOOR", pack(np.asarray(of).reshape(-1), 9))) + \
        LISTC("block_entities", [[S("id", "minecraft:chest")]])
    return C("", body)


def chunk_116_water(surface=62, floor=50):
    """1.16 chunk ("Level" wrapper, min y 0): stone to floor, water up to the surface."""
    pal = ["minecraft:air", "minecraft:stone", "minecraft:water"]
    secs = []
    for sy in range(0, 16):
        idx = np.zeros(4096, dtype=int)
        for y in range(16):
            wy = sy * 16 + y
            idx[y * 256:(y + 1) * 256] = 1 if wy <= floor else (2 if wy <= surface else 0)
        if idx.any():
            secs.append([B("Y", sy), LISTC("Palette", [[S("Name", p)] for p in pal]), L("BlockStates", pack(idx, 4))])
    hm = np.full(256, surface + 1)
    of = np.full(256, floor + 1)
    lvl = C("Level", S("Status", "full"), LISTC("Sections", secs),
            C("Heightmaps", L("MOTION_BLOCKING", pack(hm, 9)), L("OCEAN_FLOOR", pack(of, 9))))
    return C("", I("DataVersion", 2586) + lvl)


def chunk_nether(floor=40):
    pal = ["minecraft:air", "minecraft:netherrack", "minecraft:bedrock"]
    secs = []
    for sy in range(0, 8):
        idx = np.zeros(4096, dtype=int)
        for y in range(16):
            wy = sy * 16 + y
            idx[y * 256:(y + 1) * 256] = 2 if wy >= 123 else (1 if wy <= floor else 0)
        secs.append([B("Y", sy), C("block_states", LISTC("palette", [[S("Name", p)] for p in pal]), L("data", pack(idx, 4)))])
    hm = np.full(256, 128)
    return C("", I("DataVersion", 3955) + S("Status", "minecraft:full") + LISTC("sections", secs) + C("Heightmaps", L("MOTION_BLOCKING", pack(hm, 9))))


def write_region(path, chunks: dict):
    """chunks: {(local_x, local_z): nbt_bytes} -> a region file (zlib-compressed chunks)."""
    header = bytearray(8192)
    body = bytearray()
    sector = 2
    for (x, z), nbt in chunks.items():
        data = zlib.compress(nbt)
        blob = struct.pack(">IB", len(data) + 1, 2) + data
        blob += b"\x00" * (-len(blob) % 4096)
        n = len(blob) // 4096
        i = (z * 32 + x) * 4
        header[i:i + 3] = sector.to_bytes(3, "big"); header[i + 3] = n
        body += blob
        sector += n
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(header) + bytes(body))


# ------------------------------------------------------------------ decoding
def test_unpack_both_layouts():
    rng = np.random.default_rng(7)
    for bits, n in ((4, 4096), (5, 4096), (9, 256), (12, 4096)):
        vals = rng.integers(0, 1 << bits, n)
        assert (flatmap.unpack(np.array(pack(vals, bits), dtype=np.uint64), bits, n) == vals).all()
        if -(-n * bits // 64) != -(-n // (64 // bits)):
            assert (flatmap.unpack(np.array(pack(vals, bits, tight=True), dtype=np.uint64), bits, n) == vals).all()


def test_chunk_columns_modern_and_old():
    top = np.full((16, 16), 70); top[:, 8:] = 80
    names, h, d = flatmap.chunk_columns(flatmap.parse_nbt(chunk_118(top)), "overworld")
    assert names[0, 0] == "minecraft:grass_block" and h[0, 0] == 70 and h[0, 15] == 80
    names, h, d = flatmap.chunk_columns(flatmap.parse_nbt(chunk_116_water()), "overworld")
    assert names[3, 3] == "minecraft:water" and h[3, 3] == 62 and d[3, 3] == 12


def test_nether_ignores_the_bedrock_roof():
    names, h, _ = flatmap.chunk_columns(flatmap.parse_nbt(chunk_nether(40)), "nether")
    assert names[5, 5] == "minecraft:netherrack" and h[5, 5] == 40


def test_unfinished_chunks_are_skipped():
    raw = chunk_118(70, status="minecraft:noise")
    assert flatmap.chunk_columns(flatmap.parse_nbt(raw), "overworld") is None


def test_render_region(tmp_path):
    hills = np.tile(np.arange(16) + 64, (16, 1)).T            # rises to the south
    path = str(tmp_path / "r.0.0.mca")
    write_region(path, {(0, 0): chunk_118(hills), (1, 0): chunk_116_water(62, 58), (2, 0): chunk_116_water(62, 30)})
    img = flatmap.render_region(path, "overworld", flatmap.Palette())
    assert img.shape == (512, 512, 4)
    assert img[0:16, 0:48, 3].min() == 255 and img[0:16, 48:, 3].max() == 0 and img[16:, :, 3].max() == 0
    grass = img[5, 5, :3].astype(int)
    assert grass[1] > grass[0] and grass[1] > grass[2]          # green
    shallow, deep = img[5, 20, :3].astype(int), img[5, 40, :3].astype(int)
    assert shallow[2] > shallow[0] and deep[2] > deep[0]        # blue
    assert deep.sum() < shallow.sum()                            # deeper water is darker
    assert img[8, 5, :3].astype(int).sum() > img[0, 5, :3].astype(int).sum() - 1   # relief shading varies
    out_full, out_small = str(tmp_path / "t" / "full" / "0.0.png"), str(tmp_path / "t" / "small" / "0.0.png")
    flatmap.save_tiles(img, out_full, out_small)
    from PIL import Image
    assert Image.open(out_full).size == (512, 512) and Image.open(out_small).size == (64, 64)


def test_palette_textures_tints_keywords_and_hash(tmp_path):
    from PIL import Image
    jar = tmp_path / "mod.jar"
    with zipfile.ZipFile(jar, "w") as z:
        for name, col in (("assets/create/textures/block/andesite_casing.png", (150, 150, 140, 255)),
                          ("assets/minecraft/textures/block/grass_block_top.png", (150, 150, 150, 255)),
                          ("assets/create/textures/block/glass_top.png", (0, 0, 0, 0))):
            buf = io.BytesIO(); Image.new("RGBA", (16, 16), col).save(buf, "PNG"); z.writestr(name, buf.getvalue())
    tex = flatmap.jar_texture_colors(str(jar))
    assert tex["create"]["andesite_casing"][:3] == [150, 150, 140] and "glass_top" not in tex.get("create", {})
    p = flatmap.Palette(tex)
    assert p.color("create:andesite_casing") == (150, 150, 140)
    g = p.color("minecraft:grass_block")
    assert g[1] > g[0] and g[1] > g[2]                           # grey texture gets the grass tint
    assert p.color("minecraft:red_concrete") == flatmap._DYES["red"]
    assert p.color("minecraft:mossy_cobblestone") == dict(flatmap._KEYWORDS)["moss"]
    assert p.color("weirdmod:thing") == flatmap.Palette().color("weirdmod:thing")   # stable made-up colour


# ------------------------------------------------------------------ scheduling, tiles, routes
@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "servers"))
    monkeypatch.setattr(mapper, "ROOT", str(tmp_path / "servers" / ".maps"))
    base = tmp_path / "servers" / "aoc"
    (base / "world" / "region").mkdir(parents=True)
    (base / "world" / "level.dat").write_bytes(b"x")
    (base / "server.properties").write_text("motd=hi\nlevel-name=world\n")
    return base


def test_dimensions_vanilla_and_paper_layouts(world):
    (world / "world" / "DIM-1" / "region").mkdir(parents=True)
    assert [d["id"] for d in mapper.dimensions("aoc")] == ["overworld", "nether"]
    (world / "world_the_end" / "DIM1" / "region").mkdir(parents=True)
    assert {d["id"]: d["world"] for d in mapper.dimensions("aoc")}["end"] == "world_the_end"


def test_due_only_when_world_changed(world):
    mca = world / "world" / "region" / "r.0.0.mca"
    s = {"id": "aoc", "map": {}}
    assert not mapper.due(s)
    mca.write_bytes(b"x")
    assert mapper.due(s)
    s["map"] = {"rendered": time.time() + 5, "started": time.time()}
    assert not mapper.due(s)
    os.utime(mca, (time.time() + 60, time.time() + 60))
    s["map"] = {"rendered": time.time() - 30, "started": time.time() - 30}
    assert not mapper.due(s)
    s["map"]["started"] = time.time() - mapper.INTERVAL_MIN * 60 - 1
    assert mapper.due(s)
    s["map"].update(status="error", started=time.time() - 120)
    assert not mapper.due(s)


class _Alloc:
    def __init__(self): self.burst, self.home = {}, {}
    def lend_burst(self, key, n, m): self.burst[key] = [0]; return [0]
    def assign_home(self, key, n): return [0]
    def return_burst(self, key): self.burst.pop(key, None)
    def release(self, key): self.burst.pop(key, None); self.home.pop(key, None)
    def cpus_for(self, key): return [0]


def test_render_draws_only_changed_regions_and_beyond_pregen(world, tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(topology, "_allocator", _Alloc())
    state.put({"id": "aoc", "name": "aoc", "phase": "stopped", "mc_version": None})
    reg = world / "world" / "region"
    write_region(str(reg / "r.0.0.mca"), {(0, 0): chunk_118(70)})
    write_region(str(reg / "r.-20.15.mca"), {(31, 31): chunk_118(90)})     # far outside any pre-gen square
    ok = asyncio.run(mapper.render("aoc", lambda sid: None))
    m = state.get("aoc")["map"]
    assert ok and m["status"] == "ready" and m["percent"] == 100, m
    idx = mapper.public_index("aoc")["dims"]["overworld"]
    assert sorted((x, z) for x, z, _ in idx) == [(-20, 15), (0, 0)]
    assert os.path.exists(mapper.tile_path("aoc", "overworld", "small", -20, 15))
    assert mapper.changed_regions("aoc") == []
    os.utime(reg / "r.0.0.mca", (time.time() + 100, time.time() + 100))
    assert [t[1:3] for t in mapper.changed_regions("aoc")] == [[0, 0]]
    os.remove(reg / "r.-20.15.mca")                                           # world reset: its tile goes too
    asyncio.run(mapper.render("aoc", lambda sid: None))
    assert [(x, z) for x, z, _ in mapper.public_index("aoc")["dims"]["overworld"]] == [(0, 0)]
    assert not os.path.exists(mapper.tile_path("aoc", "overworld", "full", -20, 15))


def _nbt_level(spawn=None, new_style=None):
    body = I("DataVersion", 3955) + S("LevelName", "world")
    body += b"\x09" + _s("ServerBrands") + b"\x08" + struct.pack(">i", 1) + _s("vanilla")
    body += L("Longs", [0, 0])
    if spawn:
        body += I("SpawnX", spawn[0]) + I("SpawnY", 64) + I("SpawnZ", spawn[1])
    if new_style:
        body += b"\x0a" + _s("spawn") + b"\x0b" + _s("pos") + struct.pack(">i", 3) + struct.pack(">3i", new_style[0], 70, new_style[1]) + b"\x00"
    return C("", C("Data", body))


def test_spawn_point_from_level_dat(world):
    (world / "world" / "level.dat").write_bytes(gzip.compress(_nbt_level(spawn=(-112, 304))))
    assert mapper.spawn_point("aoc") == (-112, 304)
    (world / "world" / "level.dat").write_bytes(gzip.compress(_nbt_level(new_style=(48, -16))))
    os.utime(world / "world" / "level.dat", (time.time() + 5, time.time() + 5))
    assert mapper.spawn_point("aoc") == (48, -16)
    (world / "world" / "level.dat").write_bytes(b"not nbt")
    os.utime(world / "world" / "level.dat", (time.time() + 10, time.time() + 10))
    assert mapper.spawn_point("aoc") == (0, 0)


@pytest.fixture
def client(world, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    state.put({"id": "aoc", "name": "aoc", "phase": "online", "pregen": {"stage": "background", "percent": 10}})
    users._fails.clear(); users.ensure_bootstrap()
    c = TestClient(main.app)
    assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
    os.makedirs(os.path.dirname(mapper.tile_path("aoc", "overworld", "full", 0, 0)), exist_ok=True)
    open(mapper.tile_path("aoc", "overworld", "full", 0, 0), "wb").write(b"\x89PNG fake")
    mapper.write_index("aoc", {"dims": {"overworld": {"0,0": [1.0, 123, False], "1,0": [1.0, 124, True]}}})
    return c


def test_map_routes(client, tmp_path):
    idx = client.get("/api/servers/aoc/map").json()
    assert idx["dims"] == {"overworld": [[0, 0, 123]]} and idx["spawn"] == {"x": 0, "z": 0}
    assert client.get("/map/aoc/overworld/full/0.0.png").content.startswith(b"\x89PNG")
    assert client.get("/map/aoc/overworld/full/5.5.png").status_code == 404
    assert client.get("/map/aoc/overworld/full/..%2F..%2Findex.json").status_code == 404
    assert client.get("/map/aoc/../full/0.0.png").status_code == 404
    assert client.get("/map/aoc/overworld/huge/0.0.png").status_code == 404
    client.cookies.clear()
    assert client.get("/map/aoc/overworld/full/0.0.png").status_code == 401


def test_player_positions_from_rcon(client, monkeypatch):
    answers = {
        "data get entity Steve Pos": "Steve has the following entity data: [12.5d, 70.0d, -3.25d]",
        "data get entity Steve Rotation": "Steve has the following entity data: [90.0f, 10.5f]",
        "data get entity Steve Dimension": 'Steve has the following entity data: "minecraft:overworld"',
        "data get entity Alex Pos": "Alex has the following entity data: [7500.0d, 40.0d, 1.0d]",
        "data get entity Alex Rotation": "Alex has the following entity data: [0.0f, 0.0f]",
        "data get entity Alex Dimension": 'Alex has the following entity data: "minecraft:the_nether"',
    }
    async def fake(s, cmd):
        return answers[cmd]
    monkeypatch.setattr(servers, "arcmd", fake)
    main._stats["aoc"] = {"players": ["Steve", "Alex"]}
    main._pos_cache.clear()
    ps = client.get("/api/servers/aoc/map/players").json()
    assert ps[0] == {"name": "Steve", "dim": "overworld", "x": 12.5, "y": 70.0, "z": -3.25, "yaw": 90.0}
    assert ps[1]["dim"] == "nether" and ps[1]["x"] == 7500.0
    main._stats.pop("aoc", None)


def test_biome_colours():
    cells = np.array([(1 if (i % 4) >= 2 else 0) for i in range(64)])        # east half of the chunk is dark forest
    data = np.array(pack(cells, 1), dtype=np.uint64).view(np.int64)
    nbt = {"DataVersion": 3955, "sections": [{"Y": 4, "biomes": {"palette": ["minecraft:plains", "minecraft:dark_forest"], "data": data}}]}
    b = flatmap.chunk_biomes(nbt, "overworld", np.full((16, 16), 70))
    assert b[0, 0] == "minecraft:plains" and b[0, 15] == "minecraft:dark_forest" and b[15, 8] == "minecraft:dark_forest"
    assert flatmap.chunk_biomes(nbt, "nether", np.full((16, 16), 70)) is None
    plains, dark = flatmap.biome_colors("minecraft:plains"), flatmap.biome_colors("minecraft:dark_forest")
    assert sum(dark[0]) < sum(plains[0])                                        # dark forest grass is darker
    assert flatmap.biome_colors("minecraft:frozen_ocean")[2] != plains[2]       # cold water is a different blue
    assert flatmap.biome_colors("terralith:snowy_maple_forest") == flatmap.biome_colors("minecraft:snowy_plains")
    assert flatmap.biome_colors("somemod:nothing_special") == plains
    p = flatmap.Palette({"minecraft": {"grass_block_top": [150, 150, 150, 1.0], "stone": [125, 125, 125, 1.0]}})
    p.color("minecraft:grass_block"); p.color("minecraft:stone")
    assert p.tinted["minecraft:grass_block"][0] == "grass" and "minecraft:stone" not in p.tinted


def test_player_heads(monkeypatch, tmp_path):
    from app import heads
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 50
    calls = []

    class R:
        def __init__(self, code, content): self.status_code, self.content = code, content

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url):
            calls.append(url)
            return R(200, png) if "Steve" in url else R(404, b"")
    monkeypatch.setattr(heads.httpx, "AsyncClient", Client)

    async def go():
        a = await heads.face("Steve")
        b = await heads.face("Steve")            # cached: no second download
        c = await heads.face("nobody_here")
        d = await heads.face("nobody_here")      # a miss is remembered too
        e = await heads.face(".BedrockGuy")      # not a Java name: no lookup at all
        return a, b, c, d, e
    a, b, c, d, e = asyncio.run(go())
    assert a and a == b and open(a, "rb").read() == png
    assert c is None and d is None and e is None
    assert len(calls) == 2

    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    users._fails.clear(); users.ensure_bootstrap()
    client = TestClient(main.app)
    assert client.get("/api/heads/Steve.png").status_code == 401
    assert client.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
    r = client.get("/api/heads/Steve.png")
    assert r.status_code == 200 and r.content == png
    assert client.get("/api/heads/nobody_here.png").status_code == 404


def test_public_map_link(client, tmp_path):
    c = client
    tile = mapper.tile_path("aoc", "overworld", "full", 0, 0)
    open(tile, "wb").write(b"\x89PNG\r\n\x1a\nfake")
    mapper.write_index("aoc", {"dims": {"overworld": {"0,0": [1, 5, False]}}})
    r = c.put("/api/servers/aoc/public-map", json={"enabled": True, "players": False, "spawn": True})
    assert r.status_code == 200
    tok = r.json()["token"]
    anon = TestClient(main.app)                                        # no sign-in at all
    assert "<canvas" in anon.get(f"/m/{tok}").text
    info = anon.get(f"/m/{tok}/info").json()
    assert info["name"] == "aoc" and info["dims"]["overworld"] == [[0, 0, 5]] and info["spawn"] and info["players"] is False
    assert anon.get(f"/m/{tok}/tile/overworld/full/0.0.png").content.startswith(b"\x89PNG")
    assert anon.get(f"/m/{tok}/players").json() == []                 # players hidden
    assert anon.get(f"/m/{tok}/head/Steve.png").status_code == 404
    assert anon.get(f"/m/{tok}/tile/../../etc/full/0.0.png").status_code == 404
    assert anon.get("/m/wrongtoken123/info").status_code == 404
    # a new link retires the old one; turning it off hides it
    new = c.put("/api/servers/aoc/public-map", json={"enabled": True, "new_link": True}).json()["token"]
    assert new != tok and anon.get(f"/m/{tok}/info").status_code == 404 and anon.get(f"/m/{new}/info").status_code == 200
    c.put("/api/servers/aoc/public-map", json={"enabled": False})
    assert anon.get(f"/m/{new}/info").status_code == 404 and anon.get(f"/m/{new}").status_code == 404
    assert anon.put("/api/servers/aoc/public-map", json={"enabled": True}).status_code == 401


def test_every_generated_chunk_is_drawn(tmp_path):
    """Anything with terrain is drawn, whatever step the game saved it at, even a lone chunk."""
    path = str(tmp_path / "r.0.0.mca")
    write_region(path, {(0, 0): chunk_118(70, status="minecraft:features"), (5, 5): chunk_118(70, status="minecraft:light"),
                        (20, 20): chunk_118(70, status="minecraft:full"), (9, 9): chunk_118(70, status="minecraft:noise")})
    a = flatmap.render_region(path, "overworld", flatmap.Palette())[..., 3]
    assert a[0:16, 0:16].all() and a[80:96, 80:96].all() and a[320:336, 320:336].all()
    assert not a[144:160, 144:160].any()                    # started but no surface yet: nothing to draw


def test_maps_from_an_older_renderer_are_redrawn(tmp_path, monkeypatch):
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    monkeypatch.setattr(mapper, "ROOT", str(tmp_path / ".maps"))
    os.makedirs(tmp_path / "w" / "world" / "region")
    open(tmp_path / "w" / "world" / "level.dat", "wb").write(gzip.compress(_nbt_level()))
    write_region(str(tmp_path / "w" / "world" / "region" / "r.0.0.mca"), {(0, 0): chunk_118(70)})
    mt = os.path.getmtime(tmp_path / "w" / "world" / "region" / "r.0.0.mca")
    mapper.write_index("w", {"dims": {"overworld": {"0,0": [mt, 1, False]}}})            # drawn before versions existed
    assert len(mapper.changed_regions("w")) == 1 and mapper.outdated("w")
    mapper.write_index("w", {"renderer": mapper.RENDERER, "dims": {"overworld": {"0,0": [mt, 1, False]}}})
    assert mapper.changed_regions("w") == [] and not mapper.outdated("w")


def test_update_map_redraws_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    monkeypatch.setattr(mapper, "ROOT", str(tmp_path / ".maps"))
    os.makedirs(tmp_path / "w" / "world" / "region")
    open(tmp_path / "w" / "world" / "level.dat", "wb").write(gzip.compress(_nbt_level()))
    write_region(str(tmp_path / "w" / "world" / "region" / "r.0.0.mca"), {(0, 0): chunk_118(70)})
    mt = os.path.getmtime(tmp_path / "w" / "world" / "region" / "r.0.0.mca")
    mapper.write_index("w", {"renderer": mapper.RENDERER, "dims": {"overworld": {"0,0": [mt, 1, False]}}})
    assert mapper.changed_regions("w") == []
    assert len(mapper.changed_regions("w", everything=True)) == 1


def test_one_bad_chunk_does_not_lose_the_region(tmp_path, monkeypatch):
    path = str(tmp_path / "r.0.0.mca")
    write_region(path, {(0, 0): chunk_118(70), (1, 0): chunk_118(70)})
    orig = flatmap.biome_colors
    calls = {"n": 0}

    def flaky(name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("odd biome")
        return orig(name)
    monkeypatch.setattr(flatmap, "biome_colors", flaky)
    img = flatmap.render_region(path, "overworld", flatmap.Palette())
    assert img is not None and img[0, 16:32, 3].all()       # the other chunk is still drawn
