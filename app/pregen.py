"""World pre-generation.

Stage 1 ("spawn"): server is closed to players and borrows extra core groups;
the area around spawn is generated as fast as possible.
Stage 2 ("background"): the server opens and generation keeps going outward on the
borrowed cores. The game's 'Server thread' stays pinned to the server's own cores,
worker threads run at lower priority, and generation pauses whenever the slowest
ticks get long so players never feel it. When done, the borrowed cores go back to
the pool without a restart.

Two ways to generate (pregen["method"]):
- "chunky": the Chunky plugin/mod, added automatically (Paper, Purpur, Fabric, Quilt,
  Forge, NeoForge on versions Chunky supports). Fastest.
- "forceload": Minecraft's own /forceload command (1.14+), for vanilla and packs where
  Chunky isn't available. SpawnPoint loads one block of chunks at a time, waits until
  the game has finished them (read from the region files), then moves on.
Older versions without either (like 1.12.2) are "skipped".
"""
import asyncio
import logging
import os
import time

from . import config, dockerctl, hostinfo, mapper, rcon, servers, state, topology

log = logging.getLogger("spawnpoint.pregen")
POLL_S = 10
HOLD_S = 60                      # stay paused / running at least this long


def _pg(sid: str, **fields) -> dict:
    s = state.get(sid)
    s["pregen"].update(fields)
    state.save()
    return s


async def _chunky(s: dict, *cmds: str) -> list[str]:
    outs = []
    for c in cmds:
        out = await servers.arcmd(s, f"chunky {c}")
        if "Unknown" in out or "Incorrect argument" in out and c == cmds[0]:
            raise rcon.RconError(f"Chunky not installed: {out.strip()[:120]}")
        outs.append(out)
        await asyncio.sleep(0.5)
    return outs


async def _chunky_fresh(s: dict, radius: int) -> None:
    """Start SpawnPoint's own task. A world can already have a Chunky task saved (an
    imported world from another host, say, often with a far bigger radius). Chunky then
    waits for "chunky confirm" before starting, and "continue" would resume that old task,
    so cancel it first and confirm whatever Chunky asks."""
    out = (await _chunky(s, "cancel minecraft:overworld"))[0]
    if "confirm" in out.lower():
        await _chunky(s, "confirm")
    outs = await _chunky(s, "world minecraft:overworld", "spawn", "shape square", f"radius {radius}", "start")
    if "confirm" in outs[-1].lower():
        await _chunky(s, "confirm")
    _pg(s["id"], chunky_task=radius, percent=0.0)


async def _map_now(s: dict) -> None:
    """Save the new chunks to disk and have the map drawn soon, rather than at the next interval."""
    try:
        await servers.arcmd(s, "save-all")
    except rcon.RconError:
        pass
    mapper.nudge(s["id"])


async def _tick(s: dict) -> dict | None:
    for cmd in ("tick query", "spark tps"):
        try:
            v = rcon.parse_tick(await servers.arcmd(s, cmd))
            if v is not None:
                return v
        except rcon.RconError:
            return None
    return None


async def _players(s: dict) -> int:
    try:
        return rcon.parse_players(await servers.arcmd(s, "list"))[0]
    except (rcon.RconError, ValueError, TypeError):
        return 0


def _progress(line: str) -> float | None:
    import re
    m = re.search(r"([\d.]+)%", line)
    return float(m.group(1)) if m else None


class Throttle:
    """When to pause generation and when to carry on. Shared by both methods so they
    behave the same: pause on slow ticks (95th percentile, two readings in a row), on a
    nearly full disk, when someone pressed Pause, or (opt-in) while anyone is online;
    hold either state at least HOLD_S so it doesn't flap."""

    def __init__(self, sid: str):
        self.sid = sid
        self.paused = False
        self.hi = self.lo = 0
        self.since = time.monotonic() - HOLD_S
        self.empty_since = time.monotonic() - HOLD_S

    async def step(self, s: dict) -> str | None:
        """Read the server and return "pause", "resume" or None (no change)."""
        sid = self.sid
        t = await _tick(s)
        online = await _players(s)
        p95 = t["p95"] if t else None
        _pg(sid, mspt=t["avg"] if t else None, p95=p95)
        cur = state.get(sid) or {}
        playing = online > 0 and cur.get("pregen_pause_when_online") is True
        self.empty_since = time.monotonic() if online else self.empty_since
        self.hi = self.hi + 1 if p95 is not None and p95 > config.MSPT_PAUSE else 0
        self.lo = self.lo + 1 if p95 is not None and p95 < config.MSPT_RESUME else 0
        held_long = time.monotonic() - self.since >= HOLD_S
        full = hostinfo.disk_level() == "critical"
        by_hand = bool((cur.get("pregen") or {}).get("held"))
        want_pause = by_hand or playing or full or (self.hi >= 2 and held_long)
        want_resume = (not by_hand and not playing and not full and time.monotonic() - self.empty_since >= HOLD_S
                       and (p95 is None or (self.lo >= 2 and held_long)))
        if not self.paused and want_pause:
            self.paused, self.since = True, time.monotonic()
            _pg(sid, paused=True, paused_for="hand" if by_hand else "players" if playing else "disk" if full else "lag")
            return "pause"
        if self.paused and want_resume:
            self.paused, self.since = False, time.monotonic()
            _pg(sid, paused=False, paused_for=None)
            return "resume"
        return None


async def _wait_task(s: dict, after: int, throttle: bool) -> bool:
    """Wait for Chunky's 'Task finished', tracking progress and (optionally)
    pausing/continuing to keep the game smooth."""
    sid = s["id"]
    h = dockerctl.hub(sid)
    seq = after
    th = Throttle(sid)
    while True:
        for n, line in h.since(seq):
            seq = n
            if "[Chunky]" in line or "Chunky" in line:
                if "Task finished" in line or "Task completed" in line:
                    _pg(sid, percent=100.0, paused=False)
                    return True
                if (p := _progress(line)) is not None:
                    _pg(sid, percent=p)
        if dockerctl.status(sid) != "running":
            return False
        if throttle:
            d = await th.step(s)
            if d == "pause":
                await _chunky(s, "pause")
            elif d == "resume":
                await _chunky(s, "continue")
        await asyncio.sleep(POLL_S)


# ------------------------------------------------------------------ /forceload generation
BLOCK = 12                     # chunks per side of one batch (144 chunks; the game allows 256 per command)
BLOCK_TIMEOUT_S = 240          # give up on a batch after this long and move on
CHECK_S = 3


def _blocks(cx: int, cz: int, radius: int) -> list[tuple[int, int, int, int]]:
    """Batches of chunks (x1, z1, x2, z2 in chunk coordinates, inclusive) covering the
    square of `radius` blocks around chunk (cx, cz), nearest to spawn first."""
    r = -(-radius // 16)
    out = []
    for bx in range(cx - r, cx + r + 1, BLOCK):
        for bz in range(cz - r, cz + r + 1, BLOCK):
            out.append((bx, bz, min(bx + BLOCK - 1, cx + r), min(bz + BLOCK - 1, cz + r)))
    return sorted(out, key=lambda b: (max(abs((b[0] + b[2]) / 2 - cx), abs((b[1] + b[3]) / 2 - cz)), b[0], b[1]))


def _missing(sid: str, b: tuple[int, int, int, int]) -> int:
    """How many chunks of a batch aren't finished on disk yet."""
    from . import flatmap
    region_dir = mapper.region_dir(sid)
    by_region: dict[tuple[int, int], set] = {}
    for x in range(b[0], b[2] + 1):
        for z in range(b[1], b[3] + 1):
            by_region.setdefault((x >> 5, z >> 5), set()).add((x & 31, z & 31))
    missing = 0
    for (rx, rz), want in by_region.items():
        st = flatmap.chunk_statuses(os.path.join(region_dir, f"r.{rx}.{rz}.mca"), want)
        missing += sum(1 for c in want if not flatmap.is_finished(st.get(c, "")))
    return missing


async def forceload_supported(s: dict) -> bool:
    try:
        out = await servers.arcmd(s, "forceload query")
    except rcon.RconError:
        return False
    return "Unknown" not in out and "Incorrect" not in out


IN_FLIGHT = 4                  # batches loaded at once, so the game's world-gen threads all have work


async def forceload_gen(s: dict, radius: int, throttle: bool) -> bool:
    """Generate the square of `radius` blocks around spawn with /forceload, a few batches
    at a time. Resumable: batches already finished on disk are skipped. Returns True when done."""
    sid = s["id"]
    sx, sz = mapper.spawn_point(sid)
    blocks = _blocks(sx >> 4, sz >> 4, radius)
    th = Throttle(sid)
    pending = list(enumerate(blocks))
    live: dict[int, tuple] = {}                          # index -> (batch, deadline)
    finished = 0

    def area(b):
        return b[0] * 16, b[1] * 16, b[2] * 16 + 15, b[3] * 16 + 15

    async def unload(b):
        try:
            await servers.arcmd(s, "forceload remove %d %d %d %d" % area(b))
        except rcon.RconError:
            pass

    try:
        n = 0
        while pending or live:
            if dockerctl.status(sid) != "running":
                return False
            if throttle and n % 2 == 0:
                await th.step(s)
            paused = throttle and th.paused
            # top up the batches being generated (none while paused: the loaded ones finish)
            while not paused and pending and len(live) < IN_FLIGHT:
                i, b = pending.pop(0)
                if not await asyncio.to_thread(_missing, sid, b):
                    finished += 1
                    continue
                await servers.arcmd(s, "forceload add %d %d %d %d" % area(b))
                live[i] = (b, time.monotonic() + BLOCK_TIMEOUT_S)
            _pg(sid, percent=round(finished / len(blocks) * 100, 2))
            if not live:
                await asyncio.sleep(POLL_S if paused else 0)
                n += 1
                continue
            await asyncio.sleep(CHECK_S)
            n += 1
            if n % 2 == 0:
                await servers.arcmd(s, "save-all")         # finished chunks reach the region files
                await asyncio.sleep(1)
                for i, (b, deadline) in list(live.items()):
                    if not await asyncio.to_thread(_missing, sid, b) or time.monotonic() > deadline:
                        await unload(b)
                        del live[i]
                        finished += 1
    except rcon.RconError:
        return False
    finally:
        for b, _ in live.values():
            await unload(b)
    _pg(sid, percent=100.0)
    return True


async def _generate(s: dict, radius: int, throttle: bool, fresh_task: bool) -> bool | None:
    """Generate with whichever method this server has. None = it can't pre-generate."""
    sid = s["id"]
    method = s["pregen"].get("method")
    if method in (None, "chunky"):
        seq = dockerctl.hub(sid).seq
        p = s["pregen"]
        # Only "continue" a task SpawnPoint started with this radius. Servers made before
        # this was recorded count as ours, unless they were imported.
        ours = p.get("chunky_task") == radius or (p.get("chunky_task") is None and not s.get("imported"))
        try:
            if fresh_task or not ours:
                await _chunky_fresh(s, radius)
            else:
                await _chunky(s, "continue")
            _pg(sid, method="chunky")
            if state.get(sid)["pregen"].get("held"):
                await _chunky(s, "pause")
            return await _wait_task(s, seq, throttle)
        except rcon.RconError as e:
            log.info("[%s] Chunky unavailable (%s)", sid, e)
    if await forceload_supported(s):
        if s["pregen"].get("method") != "forceload":
            log.info("[%s] pre-generating with /forceload (no Chunky for this server)", sid)
            _pg(sid, method="forceload")
        return await forceload_gen(state.get(sid), radius, throttle)
    return None


async def run(sid: str, fresh: bool) -> None:
    """Boot with borrowed cores, pre-generate spawn while closed, then open and
    continue in the background."""
    s = state.get(sid)
    p = s["pregen"]
    if p["stage"] == "background" and p.get("method") == "forceload":
        # Without Chunky only the spawn area is pre-generated: /forceload works on the game's
        # main thread, so the wider world would cost lag for little gain. The rest generates
        # as people explore, which vanilla handles easily.
        p.update(stage="done", spawn_only=True, paused=False, held=False)
        state.save()
    stage = p["stage"]
    if stage in ("done", "skipped"):
        if await servers.boot_ready(sid, config.INSTALL_TIMEOUT_S if s.get("pack_pending") else 600):
            state.update(sid, pack_pending=False)
            await servers.go_online(sid)
            if state.get(sid)["pregen"]["stage"] == "background":
                # Chunky was only just added (it used to be lost on restart): start now.
                await background(sid, start_new=True)
        else:
            await servers.start_failed(sid, "Server didn't start. Check the console.")
        return
    spawn_first = stage in ("pending", "spawn")
    servers.set_phase(sid, "pregen-spawn" if spawn_first else "starting",
                      "Generating the area around spawn before opening" if spawn_first else "Starting")
    if not await servers.boot_ready(sid, config.INSTALL_TIMEOUT_S, burst=True):
        await servers.start_failed(sid, "Server didn't start. Check the console.")
        return
    state.update(sid, pack_pending=False)
    asyncio.create_task(servers._pin(s))
    s = state.get(sid)
    new_task = False
    if s["pregen"]["stage"] in ("pending", "spawn"):
        new_task = True
        _pg(sid, stage="spawn", percent=0.0)
        ok = await _generate(s, s["pregen"]["spawn_radius"], throttle=False, fresh_task=True)
        if ok is None and await asyncio.to_thread(servers.has_mod, state.get(sid), "chunky"):
            # Chunky is installed but didn't answer (the server crashed or froze): that's a
            # failed start, not a server that can't pre-generate. Try again next start.
            await servers.start_failed(sid, "Server didn't answer while generating spawn. Check the console.")
            return
        if ok is None:
            log.warning("[%s] this server can't pre-generate (no Chunky, no /forceload); skipping", sid)
            _pg(sid, stage="skipped")
            topology.allocator().return_burst(sid)
            await _shrink(sid)
            await servers.go_online(sid)
            return
        if state.get(sid)["pregen"].get("method") == "forceload":
            _pg(sid, stage="done", spawn_only=True, percent=100.0)   # see above: spawn area only
            topology.allocator().return_burst(sid)
            await _shrink(sid)
            await servers.go_online(sid)
            servers.set_phase(sid, "online", "")
            await _map_now(s)
            return
        _pg(sid, stage="background", percent=0.0)
        await _map_now(s)
    await servers.go_online(sid)
    servers.set_phase(sid, "online", "Pre-generating the wider world in the background")
    # A background run with no progress yet has no Chunky task to continue.
    await background(sid, start_new=new_task or not state.get(sid)["pregen"].get("percent"))


def unskip(sid: str) -> bool:
    """An earlier start couldn't reach Chunky (the server was crashing) and gave up on
    pre-generation. If Chunky is installed, pick it up again. True if it was changed."""
    s = state.get(sid)
    p = s["pregen"]
    if p["stage"] == "skipped" and servers.has_mod(s, "chunky"):
        log.info("[%s] Chunky is installed: resuming pre-generation that was skipped", sid)
        p.update(stage="background", method="chunky")
        state.save()
        return True
    return False


async def background(sid: str, start_new: bool = False) -> None:
    s = state.get(sid)
    if s["pregen"].get("method") == "forceload":
        _pg(sid, stage="done", spawn_only=True, paused=False, held=False)   # spawn area only, see run()
        return
    alloc = topology.allocator()
    if not alloc.burst.get(sid):
        if alloc.lend_burst(sid, config.BURST_GROUPS, config.MAX_BURST_SERVERS):
            await asyncio.to_thread(dockerctl.set_cpuset, sid, alloc.cpus_for(sid))
    done = await _generate(s, s["pregen"]["full_radius"], throttle=True, fresh_task=start_new)
    if done is None and not await asyncio.to_thread(servers.has_mod, state.get(sid), "chunky"):
        _pg(sid, stage="skipped")
    elif done is None:
        log.info("[%s] Chunky didn't answer; pre-generation carries on at the next start", sid)
    if done is None or done:
        alloc.return_burst(sid)
        await _shrink(sid)  # give the borrowed cores back before reporting "done"
        if done:
            _pg(sid, stage="done", paused=False, held=False)
            servers.set_phase(sid, "online", "")
            await _map_now(s)


async def _shrink(sid: str) -> None:
    cpus = topology.allocator().cpus_for(sid)
    await asyncio.to_thread(dockerctl.set_cpuset, sid, cpus)
