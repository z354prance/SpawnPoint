"""World maps: SpawnPoint's own top-down map of each server (see flatmap.py).

- Draws every region file of the Overworld, Nether and End that exists, so land players
  explore beyond the pre-generation limit shows up too.
- Only redraws region files that changed since the last drawing, at most every
  MAP_INTERVAL_MIN minutes per server, and up to MAP_PARALLEL servers at a time.
- Runs as a separate low-priority process on borrowed spare core groups, frozen whenever
  the server's tick time climbs above MSPT_PAUSE and resumed below MSPT_RESUME.
- Tiles: <DATA_DIR>/.maps/<sid>/<dim>/{full,small}/<x>.<z>.png, one per region file.
"""
import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import time

from . import config, dockerctl, state, topology

log = logging.getLogger("spawnpoint.map")

ROOT = os.path.join(config.DATA_DIR, ".maps")
ENABLED = os.environ.get("MAP_ENABLED", "true").lower() == "true"
INTERVAL_MIN = int(os.environ.get("MAP_INTERVAL_MIN", "10"))
GROUPS = int(os.environ.get("MAP_CORE_GROUPS", "2"))
PARALLEL = max(1, int(os.environ.get("MAP_PARALLEL", "2")))   # servers drawn at the same time
_current: set[str] = set()           # sids being drawn
_procs: dict[str, asyncio.subprocess.Process] = {}


def map_dir(sid: str) -> str:
    return os.path.join(ROOT, sid)


def level_name(sid: str) -> str:
    try:
        with open(os.path.join(dockerctl.server_dir(sid), "server.properties"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("level-name="):
                    return line.split("=", 1)[1].strip() or "world"
    except OSError:
        pass
    return "world"


_SUB = {"minecraft:overworld": "", "minecraft:the_nether": "DIM-1", "minecraft:the_end": "DIM1"}
_DIMDIR = {"minecraft:overworld": "overworld", "minecraft:the_nether": "the_nether", "minecraft:the_end": "the_end"}


def _region_in(base: str, world: str, dim: str) -> str | None:
    """A dimension's region folder inside one world folder, or None. Since 26.1 every
    dimension lives under dimensions/minecraft/<name>; before that the Overworld was the
    world folder itself and the others were DIM-1 and DIM1."""
    for d in (os.path.join(base, world, "dimensions", "minecraft", _DIMDIR[dim], "region"),
              os.path.join(base, world, _SUB[dim], "region")):
        if os.path.isdir(d):
            return d
    return None


def region_dir(sid: str, dim: str = "minecraft:overworld") -> str:
    """Where a dimension's region files are (or will be, for the Overworld)."""
    base, lvl = dockerctl.server_dir(sid), level_name(sid)
    if dim != "minecraft:overworld":
        for world in (lvl + ("_nether" if dim == "minecraft:the_nether" else "_the_end"), lvl):
            hit = _region_in(base, world, dim)
            if hit:
                return hit
    hit = _region_in(base, lvl, dim)
    if hit:
        return hit
    # Not written yet: the new layout if the world already uses it
    if os.path.isdir(os.path.join(base, lvl, "dimensions")):
        return os.path.join(base, lvl, "dimensions", "minecraft", _DIMDIR[dim], "region")
    return os.path.join(base, lvl, _SUB[dim], "region")


def dimensions(sid: str) -> list[dict]:
    """Which maps exist on disk. Vanilla/Forge/Fabric keep the Nether and End inside the
    world folder (DIM-1, DIM1, or dimensions/minecraft/... since 26.1); Paper/Purpur before
    26.1 keep them in world_nether and world_the_end."""
    base, lvl = dockerctl.server_dir(sid), level_name(sid)
    out = []
    if os.path.exists(os.path.join(base, lvl, "level.dat")):
        out.append({"id": "overworld", "name": "Overworld", "world": lvl, "dim": "minecraft:overworld", "sorting": 0,
                    "sky": "#7dabff", "void": "#000000", "ambient": "0.1", "caves": "55", "ceiling": False,
                    "region": region_dir(sid, "minecraft:overworld")})
    for mid, name, folder, dim, sort, sky, void in (
            ("nether", "Nether", f"{lvl}_nether", "minecraft:the_nether", 100, "#290000", "#150000"),
            ("end", "The End", f"{lvl}_the_end", "minecraft:the_end", 200, "#080010", "#080010")):
        world, rd = folder, _region_in(base, folder, dim)          # Paper/Purpur layout
        if not rd:
            world, rd = lvl, _region_in(base, lvl, dim)
        if not rd:
            continue
        out.append({"id": mid, "name": name, "world": world, "dim": dim, "sorting": sort, "sky": sky, "void": void,
                    "ambient": "0.6", "caves": "-10000", "ceiling": mid == "nether", "region": rd})
    return out


# ------------------------------------------------------------------ world spawn
_spawn_cache: dict[str, tuple[float, tuple[int, int]]] = {}


def _nbt_find_spawn(data: bytes) -> tuple[int, int] | None:
    """Walk a level.dat (uncompressed NBT) for the spawn: Data.SpawnX/SpawnZ, or the
    newer Data.spawn.pos int array. Only reads what it needs; skips everything else."""
    import struct
    pos = 0
    found: dict[str, int] = {}

    def rd(n):
        nonlocal pos
        b = data[pos:pos + n]
        if len(b) < n:
            raise ValueError("truncated")
        pos += n
        return b

    def name():
        (n,) = struct.unpack(">H", rd(2))
        return rd(n).decode("utf-8", "replace")

    def payload(t, path):
        if t == 1: rd(1)
        elif t == 2: rd(2)
        elif t == 3:
            (v,) = struct.unpack(">i", rd(4))
            if path[-1] in ("SpawnX", "SpawnZ"):
                found[path[-1]] = v
        elif t in (4, 6): rd(8)
        elif t == 5: rd(4)
        elif t == 7: (n,) = struct.unpack(">i", rd(4)); rd(n)
        elif t == 8: name()
        elif t == 9:
            et, n = struct.unpack(">bi", rd(5))
            for _ in range(n):
                payload(et, path + ["[]"])
        elif t == 10:
            while True:
                (ct,) = struct.unpack(">b", rd(1))
                if ct == 0:
                    break
                payload(ct, path + [name()])
        elif t == 11:
            (n,) = struct.unpack(">i", rd(4))
            vals = struct.unpack(f">{n}i", rd(4 * n))
            if path[-1] == "pos" and len(path) >= 2 and path[-2] == "spawn" and n >= 3:
                found.setdefault("SpawnX", vals[0]); found.setdefault("SpawnZ", vals[2])
        elif t == 12: (n,) = struct.unpack(">i", rd(4)); rd(8 * n)
        else:
            raise ValueError(f"tag {t}")

    try:
        (t,) = struct.unpack(">b", rd(1))
        name()
        payload(t, [""])
    except (ValueError, struct.error):
        return None
    if "SpawnX" in found and "SpawnZ" in found:
        return found["SpawnX"], found["SpawnZ"]
    return None


def legacy_registry(sid: str) -> dict:
    """A modded pre-1.13 world's numeric block and biome ids (level.dat, FML registries),
    as {"blocks": {"id": "name"}, "biomes": {...}}. Empty for newer worlds."""
    import gzip
    from . import flatmap, legacy
    path = os.path.join(dockerctl.server_dir(sid), level_name(sid), "level.dat")
    try:
        with gzip.open(path) as f:
            root = flatmap.parse_nbt(f.read())
    except (OSError, ValueError, EOFError):
        return {"blocks": {}, "biomes": {}}
    blocks, biomes = legacy.registry_from_level(root)
    return {"blocks": {str(k): v for k, v in blocks.items()}, "biomes": {str(k): v for k, v in biomes.items()}}


def texture_version(mc: str | None) -> str | None:
    """Which game version to take block textures from. Before 1.13 the textures had other
    names, so old worlds (whose blocks are converted to today's names) use 1.16.5's."""
    import re as _re
    m = _re.match(r"^1\.(\d+)", mc or "")
    if m and int(m.group(1)) < 13:
        return "1.16.5"
    return mc


def spawn_point(sid: str) -> tuple[int, int]:
    """Where Chunky centres pre-generation ("chunky spawn"): the world spawn from level.dat."""
    import gzip
    path = os.path.join(dockerctl.server_dir(sid), level_name(sid), "level.dat")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return 0, 0
    hit = _spawn_cache.get(sid)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with gzip.open(path) as f:
            sp = _nbt_find_spawn(f.read()) or (0, 0)
    except OSError:
        sp = (0, 0)
    _spawn_cache[sid] = (mtime, sp)
    return sp


def world_changed_at(sid: str) -> float:
    """Newest region file across all dimensions: when the world last changed on disk."""
    base, newest = dockerctl.server_dir(sid), 0.0
    for d in dimensions(sid):
        folder = d["region"]
        try:
            for e in os.scandir(folder):
                if e.name.endswith(".mca"):
                    newest = max(newest, e.stat().st_mtime)
        except OSError:
            pass
    return newest




def region_files(sid: str) -> dict[str, dict[str, tuple[str, float]]]:
    """{dim: {"x,z": (path, mtime)}} for every region file on disk."""
    out: dict[str, dict] = {}
    base = dockerctl.server_dir(sid)
    for d in dimensions(sid):
        folder = d["region"]
        regs = {}
        try:
            for e in os.scandir(folder):
                parts = e.name.split(".")
                if len(parts) == 4 and parts[0] == "r" and parts[3] == "mca" and e.stat().st_size > 8192:
                    regs[f"{int(parts[1])},{int(parts[2])}"] = (e.path, e.stat().st_mtime)
        except (OSError, ValueError):
            pass
        out[d["id"]] = regs
    return out


def _index_path(sid: str) -> str:
    return os.path.join(map_dir(sid), "index.json")


def read_index(sid: str) -> dict:
    try:
        with open(_index_path(sid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"dims": {}}


def write_index(sid: str, idx: dict) -> None:
    os.makedirs(map_dir(sid), exist_ok=True)
    tmp = _index_path(sid) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(idx, f)
    os.replace(tmp, _index_path(sid))


def tile_path(sid: str, dim: str, lod: str, x: int, z: int) -> str:
    return os.path.join(map_dir(sid), dim, lod, f"{x}.{z}.png")


def public_index(sid: str) -> dict:
    """What the dashboard needs: which tiles exist, with a version for caching."""
    idx = read_index(sid)
    return {"dims": {d: [[*map(int, k.split(",")), v[1]] for k, v in regs.items() if not v[2]]
                     for d, regs in idx.get("dims", {}).items()}}


def has_map(sid: str) -> bool:
    return any(not v[2] for regs in read_index(sid).get("dims", {}).values() for v in regs.values())


# Bump when the drawing changes in a way old tiles should be redrawn for (every server's
# whole map is drawn again once, in the background).
RENDERER = 5


def outdated(sid: str) -> bool:
    root = read_index(sid)
    return bool(root.get("dims")) and root.get("renderer") != RENDERER


def changed_regions(sid: str, everything: bool = False) -> list:
    """Region files that are new or changed since they were last drawn (all of them when
    asked to, or when the map was drawn by an older version of the renderer)."""
    root = read_index(sid)
    idx = {} if everything or root.get("renderer") != RENDERER else root.get("dims", {})
    todo = []
    for dim, regs in region_files(sid).items():
        done = idx.get(dim, {})
        for key, (path, mtime) in regs.items():
            if key not in done or done[key][0] < mtime:
                x, z = map(int, key.split(","))
                todo.append([dim, x, z, path, tile_path(sid, dim, "full", x, z), tile_path(sid, dim, "small", x, z), mtime])
    return todo


def set_status(sid: str, **fields) -> None:
    try:
        s = state.get(sid)
    except KeyError:
        return
    m = dict(s.get("map") or {})
    m.update(fields)
    state.update(sid, map=m)


def _tidy_old(sid: str) -> None:
    """Remove what the earlier BlueMap version left behind."""
    for old in ("web", "config", "data", "heads"):
        shutil.rmtree(os.path.join(map_dir(sid), old), ignore_errors=True)


async def render(sid: str, mspt_of) -> bool:
    """Draw the regions of one server that changed. Returns True when it finished cleanly."""
    s = state.get(sid)
    alloc = topology.allocator()
    key = "map:" + sid
    groups = alloc.lend_burst(key, GROUPS, PARALLEL)
    if not groups and s.get("phase") in ("stopped", "sleeping"):
        groups = alloc.assign_home(key, 1)       # its own server isn't using any cores right now
    if not groups:
        set_status(sid, status="waiting", note="Waiting for spare CPU cores")
        alloc.return_burst(key)
        _current.discard(sid)
        return False
    cpus = alloc.cpus_for(key)
    _current.add(sid)
    started = time.time()
    try:
        await asyncio.to_thread(_tidy_old, sid)
        everything = bool((s.get("map") or {}).get("full"))      # "Update map": draw it all again
        todo = await asyncio.to_thread(changed_regions, sid, everything)
        if not todo:
            set_status(sid, status="ready", started=started, rendered=time.time(), note="")
            return True
        total = len(todo)
        set_status(sid, status="rendering", started=started, percent=0, note="", total=total)
        mods = os.path.join(dockerctl.server_dir(sid), "mods")
        job = {"cpus": cpus, "mc_version": texture_version(s.get("mc_version")), "mods_dir": mods if os.path.isdir(mods) else None,
               "cache_dir": os.path.join(ROOT, ".textures"), "todo": [t[:6] for t in todo],
               "legacy": await asyncio.to_thread(legacy_registry, sid)}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.mapjob", json.dumps(job), cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=1 << 20)
        _procs[sid] = proc
        mtimes = {(t[0], t[1], t[2]): t[6] for t in todo}
        results, done, paused, errors = [], 0, False, []
        skipped: dict[str, list] = {}                       # reason -> [chunk count, example region]

        async def watch_lag():
            nonlocal paused
            while proc.returncode is None:
                await asyncio.sleep(5)
                mspt = mspt_of(sid)
                try:
                    if not paused and mspt is not None and mspt > config.MSPT_PAUSE:
                        os.killpg(proc.pid, signal.SIGSTOP); paused = True
                        set_status(sid, status="paused", note=f"Paused while the server is busy ({mspt:.0f} ms ticks)")
                    elif paused and (mspt is None or mspt < config.MSPT_RESUME):
                        os.killpg(proc.pid, signal.SIGCONT); paused = False
                        set_status(sid, status="rendering", note="")
                except ProcessLookupError:
                    return

        watcher = asyncio.create_task(watch_lag())
        try:
            async for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "x" in msg:
                    results.append(msg)
                    done += 1
                    for why, n in (msg.get("skipped") or {}).items():
                        if why == "not generated yet":
                            continue                        # the edge of the world; that's expected
                        e = skipped.setdefault(why, [0, f"r.{msg['x']}.{msg['z']} ({msg['dim']})"])
                        e[0] += n
                    if not msg["ok"]:
                        errors.append(f"r.{msg['x']}.{msg['z']} ({msg['dim']}): {msg.get('error', '')}")
                    if done % 4 == 0 or done == total:
                        set_status(sid, percent=round(100 * done / total, 1))
            await proc.wait()
        finally:
            watcher.cancel()
            _procs.pop(sid, None)
        err_tail = (await proc.stderr.read()).decode(errors="replace").strip().splitlines()[-3:]
        idx = read_index(sid)
        idx["renderer"] = RENDERER
        stamp = int(time.time())
        for r in results:
            if r["ok"]:
                idx["dims"].setdefault(r["dim"], {})[f"{r['x']},{r['z']}"] = [mtimes[(r["dim"], r["x"], r["z"])], stamp, bool(r.get("empty"))]
        # forget tiles whose region files are gone (a world reset, say)
        live = region_files(sid)
        for dim in list(idx["dims"]):
            for k in list(idx["dims"][dim]):
                if k not in live.get(dim, {}):
                    x, z = map(int, k.split(","))
                    for lod in ("full", "small"):
                        try:
                            os.remove(tile_path(sid, dim, lod, x, z))
                        except OSError:
                            pass
                    del idx["dims"][dim][k]
        write_index(sid, idx)
        if proc.returncode != 0 and not results:
            set_status(sid, status="error", note=f"The map couldn't be drawn: {' '.join(err_tail)[-300:]}")
            log.warning("[%s] map job failed: %s", sid, "\n".join(err_tail))
            return False
        note = ""
        if errors:
            for e in errors:
                log.warning("[%s] map: %s", sid, e)
            note = f"{len(errors)} region(s) couldn't be drawn, for example {errors[0][:160]}"
        if skipped:
            parts = [f"{n} {why}, e.g. in {ex}" for why, (n, ex) in sorted(skipped.items(), key=lambda kv: -kv[1][0])[:3]]
            for p in parts:
                log.warning("[%s] map chunks skipped: %s", sid, p)
            note = (note + ". " if note else "") + "Chunks left blank: " + "; ".join(parts)
        set_status(sid, status="ready", rendered=time.time(), took=round(time.time() - started), percent=100, note=note, full=False)
        log.info("[%s] map: drew %d regions in %ds", sid, done, round(time.time() - started))
        return True
    except Exception as e:
        set_status(sid, status="error", note=f"Couldn't draw the map: {e}")
        log.warning("[%s] map render: %s", sid, e)
        return False
    finally:
        alloc.release(key)
        _current.discard(sid)


def nudge(sid: str) -> None:
    """Draw this server's map at the next check (about 30 s) instead of waiting out the interval."""
    s = state.get(sid)
    if s and isinstance(s.get("map"), dict) and s["map"].get("status") != "rendering":
        s["map"]["started"] = 0
        state.save()


def due(s: dict) -> bool:
    from . import hostinfo
    if hostinfo.disk_level() == "critical":
        return False                         # don't write tiles onto a nearly full disk
    m = s.get("map") or {}
    if m.get("off"):
        return False
    changed = world_changed_at(s["id"])
    if not changed:
        return False
    last = m.get("rendered") or 0
    tried = m.get("started") or 0
    if changed <= last and not outdated(s["id"]):
        return False
    if m.get("status") == "error" and time.time() - tried < 3600:
        return False                         # don't hammer a failing render; retry hourly
    return time.time() - tried >= INTERVAL_MIN * 60 or not last


async def loop(mspt_of) -> None:
    """Background scheduler: draws servers whose world changed, up to MAP_PARALLEL at once."""
    if not ENABLED:
        return
    await asyncio.sleep(20)
    tasks: set[asyncio.Task] = set()
    while True:
        try:
            for sid, s in list(state.servers().items()):
                if len(_current) >= PARALLEL:
                    break
                if sid in _current or s.get("phase") in ("queued", "importing", "installing", "preparing"):
                    continue
                if await asyncio.to_thread(due, s):
                    _current.add(sid)                    # claim it before the task starts
                    t = asyncio.create_task(render(sid, mspt_of))
                    tasks.add(t); t.add_done_callback(tasks.discard)
        except Exception as e:
            log.warning("map scheduler: %s", e)
        await asyncio.sleep(30)


def stop_all() -> None:
    for proc in list(_procs.values()):
        try:
            os.killpg(proc.pid, signal.SIGCONT)
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def cleanup(sid: str) -> None:
    proc = _procs.get(sid)
    if proc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    shutil.rmtree(map_dir(sid), ignore_errors=True)
