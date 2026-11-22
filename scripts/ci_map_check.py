"""CI: draw a real vanilla world with SpawnPoint's flat map and serve the tiles back.

Expects /tmp/mapci/servers/demo to hold a world made by itzg/minecraft-server.
Needs internet for the vanilla client jar (block colours), which CI has."""
import asyncio, base64, glob, io, json, os, sys, time
os.environ.update(ISOLATED_DOCKER="false", DATA_DIR="/tmp/mapci/servers", BACKUP_DIR="/tmp/mapci/backups",
                  ADMIN_PASSWORD="pw", MAP_CORE_GROUPS="2", RESERVED_CORE_GROUPS="0")
sys.path.insert(0, os.getcwd())
import numpy as np
from PIL import Image
from app import mapper, state, topology, main, users, flatmap
from fastapi.testclient import TestClient

state.load()
state.put({"id": "demo", "name": "demo", "phase": "stopped", "mc_version": "1.21.1", "loader": "vanilla",
           "hostname": "demo.example", "memory": {"max_gb": 2}, "pregen": {"stage": "done", "full_radius": 200, "spawn_radius": 100}})
print("dimensions:", mapper.dimensions("demo"))
print("spawn:", mapper.spawn_point("demo"))
assert mapper.due(state.get("demo")), "a fresh world should be due for a render"
t0 = time.time()
ok = asyncio.run(mapper.render("demo", lambda sid: None))
took = time.time() - t0
m = state.get("demo")["map"]
print("map state:", json.dumps(m))
assert ok and m["status"] == "ready", "render failed"
n_regions = sum(len(v) for v in mapper.region_files("demo").values())
print(f"drew {n_regions} regions in {took:.1f}s ({took / max(n_regions, 1):.2f}s each, {os.cpu_count()} cpus)")

tex = glob.glob(os.path.join(mapper.ROOT, ".textures", "tex-*.json"))
assert tex, "vanilla textures weren't loaded"
cols = max((json.load(open(t)).get("minecraft", {}) for t in tex), key=len)
print("texture colours:", len(cols), "| grass", cols.get("grass_block_top") or cols.get("grass_block"), "| stone", cols.get("stone"))
assert len(cols) > 300, "too few block colours from the vanilla jar"

idx = mapper.public_index("demo")
tiles = idx["dims"].get("overworld", [])
print("overworld tiles:", len(tiles), "| dims:", {d: len(t) for d, t in idx["dims"].items()})
assert tiles, "no overworld tiles"
# the region holding spawn should be varied terrain, not one flat colour
sx, sz = mapper.spawn_point("demo")
rx, rz = sx // 512, sz // 512
img = np.asarray(Image.open(mapper.tile_path("demo", "overworld", "full", rx, rz)).convert("RGBA"))
drawn = img[img[..., 3] > 0][:, :3]
uniq = len(np.unique(drawn // 8, axis=0))
print(f"spawn region r.{rx}.{rz}: {len(drawn)} pixels drawn, {uniq} distinct colours, mean {drawn.mean(axis=0).round()}")
small = Image.open(mapper.tile_path("demo", "overworld", "small", rx, rz)); assert small.size == (64, 64)

# stitch every overworld tile into one picture and print it, to compare by eye
xs = [t[0] for t in tiles]; zs = [t[1] for t in tiles]
canvas = Image.new("RGBA", ((max(xs) - min(xs) + 1) * 512, (max(zs) - min(zs) + 1) * 512), (12, 10, 24, 255))
for tx, tz, _ in tiles:
    im = Image.open(mapper.tile_path("demo", "overworld", "full", tx, tz)).convert("RGBA")
    canvas.alpha_composite(im, ((tx - min(xs)) * 512, (tz - min(zs)) * 512))
bbox = canvas.split()[3].point(lambda a: 255).getbbox()
crop = canvas.crop(Image.fromarray(np.asarray(canvas)[..., :3].sum(axis=2).astype(np.uint8) != 46).getbbox() or bbox)
buf = io.BytesIO(); crop.save(buf, "PNG", optimize=True)
print("PREVIEW", crop.size, base64.b64encode(buf.getvalue()).decode())

assert len(drawn) > 10000 and uniq > 40, "the spawn region looks blank or flat"
assert not mapper.due(state.get("demo")), "an unchanged world shouldn't render again"
assert topology.allocator().burst == {} and "map:demo" not in topology.allocator().home, "cores not returned"

users.ensure_bootstrap()
c = TestClient(main.app)
assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
api = c.get("/api/servers/demo/map").json()
assert api["dims"]["overworld"] and api["status"]["status"] == "ready"
x, z, v = api["dims"]["overworld"][0]
r = c.get(f"/map/demo/overworld/full/{x}.{z}.png?v={v}")
assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
assert c.get("/map/demo/overworld/full/999.999.png").status_code == 404
assert c.get("/map/demo/../../etc/full/0.0.png").status_code in (404, 422)
c2 = TestClient(main.app)
assert c2.get(f"/map/demo/overworld/full/{x}.{z}.png").status_code == 401, "tiles need a login"
print("flat map render and serving OK")
