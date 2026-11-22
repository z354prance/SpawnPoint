"""Server lifecycle orchestration: provision -> prepare -> spawn pregen -> online.
Player routing needs no setup step: the built-in proxy (proxy.py) looks each
hostname up in state and only forwards players to servers whose phase is 'online'."""
import asyncio
import glob
import logging
import os
import re
import secrets
import shutil
import tarfile
import time

from . import catalog, config, dockerctl, perf, rcon, router, state, topology

log = logging.getLogger("spawnpoint.servers")
_tasks: dict[str, asyncio.Task] = {}

# Helper mods/plugins added to every server if missing (Modrinth slugs).
HELPERS = {
    "paper": ["chunky", "spark"],
    "purpur": ["chunky", "spark"],
    "fabric": ["chunky", "spark", "ferrite-core", "lithium", "modernfix"],
    "quilt": ["chunky", "spark", "ferrite-core", "lithium", "modernfix"],
    "forge": ["chunky", "spark", "ferrite-core", "modernfix"],
    "neoforge": ["chunky", "spark", "ferrite-core", "modernfix"],
    "vanilla": [],
}


def mods_dir(s: dict) -> str:
    sub = "plugins" if s.get("loader") in ("paper", "purpur") else "mods"
    return os.path.join(dockerctl.server_dir(s["id"]), sub)


def mod_files(s: dict) -> list[str]:
    d = mods_dir(s)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith((".jar", ".jar.disabled")))


def is_managed(s: dict) -> bool:
    """Modpack servers: the itzg image owns the mods folder and deletes jars it didn't
    put there on every start. Extra mods must go through its MODRINTH_PROJECTS list."""
    return s.get("kind") in perf.MODPACK_KINDS


async def install_mod(s: dict, project: str) -> dict:
    """Add a Modrinth mod the way that survives restarts for this server type.
    Raises ValueError if there's no build for the server's loader and version."""
    loader, ver = s.get("loader") or "", s.get("mc_version") or ""
    if is_managed(s):
        v = await catalog.find_version(project, loader, ver)
        if not v:
            raise ValueError(f"No {loader} {ver} build of {project}")
        s = state.get(s["id"])
        mm = [m for m in s.get("managed_mods", []) if m["project_id"] != v["project_id"]]
        mm.append({k: v[k] for k in ("project_id", "slug", "version_id", "version", "filename", "title")})
        hist = sorted(set(s.get("managed_history", [])) | {v["filename"]})
        state.update(s["id"], managed_mods=mm, managed_history=hist)
        return {**v, "managed": True}
    r = await catalog.download_mod(project, loader, ver, mods_dir(s))
    return {**r, "managed": False}


def remove_managed_mod(sid: str, project_id: str) -> None:
    s = state.get(sid)
    state.update(sid, managed_mods=[m for m in s.get("managed_mods", []) if m["project_id"] != project_id])


# Mod IDs (as declared inside the jar) for the helper slugs, to spot copies a pack ships itself.
HELPER_MODIDS = {"chunky": "chunky", "spark": "spark", "ferrite-core": "ferritecore",
                 "modernfix": "modernfix", "lithium": "lithium"}


def pack_mod_ids(s: dict) -> set[str]:
    """Mod IDs of jars in the mods folder that SpawnPoint didn't add itself."""
    from . import clientmods
    # Everything we ever added, including old builds the pack installer hasn't removed yet.
    ours = {m["filename"] for m in s.get("managed_mods", [])} | set(s.get("managed_history", []))
    d = mods_dir(s)
    ids: set[str] = set()
    for f in mod_files(s):
        if f.endswith(".jar") and f not in ours:
            ids |= clientmods.jar_mod_ids(os.path.join(d, f))
    return ids


def has_mod(s: dict, slug: str, pack_ids: set[str] | None = None) -> bool:
    key = slug.replace("-", "").replace("_", "").lower()
    if any(m["slug"] == slug or key in m["slug"].replace("-", "") for m in s.get("managed_mods", [])):
        return True
    if is_managed(s):
        # A pack may ship the mod itself (by mod ID). Loose copies we added are ignored:
        # the pack installer deletes them at the next start.
        modid = HELPER_MODIDS.get(slug, key)
        return modid in (pack_ids if pack_ids is not None else pack_mod_ids(s))
    return key in " ".join(mod_files(s)).lower().replace("-", "").replace("_", "")


def refresh_memory_plan(s: dict) -> dict:
    """Servers on automatic memory get the current plan (normal size + growth ceiling)
    from their real mod count. Manual settings without a normal size keep their
    ceiling as the normal size and get room to grow above it."""
    m = s.get("memory") or {}
    if m.get("auto"):
        n = len([f for f in mod_files(s) if f.endswith(".jar")]) or s.get("mod_count")
        init, soft, mx = perf.memory_plan(s.get("kind", ""), n)
        new = {"init_gb": init, "soft_gb": soft, "max_gb": mx, "auto": True}
    elif not m.get("soft_gb"):
        new = {**m, "soft_gb": m["max_gb"], "max_gb": perf.ceiling_for(m["max_gb"])}
    else:
        return s
    if new != m:
        log.info("[%s] memory: start %s GB, normally %s GB, can grow to %s GB",
                 s["id"], new["init_gb"], new["soft_gb"], new["max_gb"])
        s = state.update(s["id"], memory=new)
    return s


async def reconcile_install(s: dict) -> dict:
    """Check the installed files for the real loader and Minecraft version. If our record
    is wrong (e.g. a CurseForge pack recorded as Forge 1.20.1 that is really NeoForge
    1.21.1), correct it and re-fetch add-on mods built for the right loader/version.
    Also drop add-ons the pack already ships, which would otherwise be duplicates."""
    from . import importer
    sid = s["id"]
    found = await asyncio.to_thread(importer.detect, dockerctl.server_dir(sid))
    kind, mc = found.get("kind"), found.get("mc_version")
    changed = {}
    if kind in ("forge", "neoforge", "fabric", "quilt", "paper", "purpur") and kind != s.get("loader"):
        changed["loader"] = kind
    if mc and mc != s.get("mc_version") and kind:
        changed["mc_version"] = mc
    pack_ids = await asyncio.to_thread(pack_mod_ids, s) if is_managed(s) else set()
    managed = list(s.get("managed_mods", []))
    if changed:
        log.warning("[%s] installed server is %s %s, not %s %s: correcting", sid,
                    changed.get("loader", s.get("loader")), changed.get("mc_version", s.get("mc_version")),
                    s.get("loader"), s.get("mc_version"))
        new_java = perf.java_tag(changed.get("mc_version", s.get("mc_version", "")))
        if perf.java_major(new_java) > perf.java_major(s.get("java_tag", "java8")):
            changed["java_tag"] = new_java
        loader = changed.get("loader", s.get("loader"))
        ver = changed.get("mc_version", s.get("mc_version"))
        fixed = []
        for m in managed:
            try:
                v = await catalog.find_version(m["slug"], loader, ver)
            except Exception:
                v = None
            if v:
                fixed.append({k: v[k] for k in ("project_id", "slug", "version_id", "version", "filename", "title")})
            else:
                log.info("[%s] dropping %s: no %s %s build", sid, m["slug"], loader, ver)
        managed = fixed
        changed["helpers_managed"] = False   # re-check helpers for the right loader
        changed["real_ip_via"] = ""
    dupes = [m for m in managed if HELPER_MODIDS.get(m["slug"]) in pack_ids]
    if dupes:
        log.info("[%s] the pack already ships %s; not adding our copies", sid, ", ".join(m["slug"] for m in dupes))
        managed = [m for m in managed if m not in dupes]
    if changed or dupes:
        changed["managed_mods"] = managed
        changed["managed_history"] = sorted(set(s.get("managed_history", []))
                                            | {m["filename"] for m in s.get("managed_mods", [])}
                                            | {m["filename"] for m in managed})
        s = state.update(sid, **changed)
    return s


async def ensure_pack_helpers(s: dict) -> None:
    """Modpack servers set up before helpers were added via MODRINTH_PROJECTS lost them
    on restart. Add any that are missing to the managed list (once per server)."""
    if not is_managed(s) or s.get("helpers_managed"):
        return
    added = []
    pack_ids = await asyncio.to_thread(pack_mod_ids, s)
    for slug in HELPERS.get(s.get("loader") or "", []):
        if has_mod(state.get(s["id"]), slug, pack_ids):
            continue
        try:
            added.append((await install_mod(state.get(s["id"]), slug))["filename"])
        except Exception as e:
            log.info("[%s] skipped helper %s: %s", s["id"], slug, e)
    st = state.get(s["id"])
    if added and st.get("pregen", {}).get("stage") == "done" and not st.get("pregen", {}).get("percent"):
        st["pregen"]["stage"] = "background"  # it was skipped because Chunky wasn't loaded
    state.update(s["id"], helpers_managed=True, pregen=st["pregen"],
                 helpers_added=sorted(set(st.get("helpers_added", []) + added)))


def set_phase(sid: str, phase: str, msg: str = "") -> None:
    before = (state.get(sid) or {}).get("phase")
    state.update(sid, phase=phase, phase_msg=msg, phase_at=time.time())
    log.info("[%s] %s %s", sid, phase, msg)
    if phase != before:
        from . import alerts
        name = (state.get(sid) or {}).get("name", sid)
        if phase == "online":
            alerts.send("server_online", name, f"{name} is online", msg, "ok")
        elif before == "online" and phase in ("stopped", "sleeping"):
            alerts.send("server_offline", name, f"{name} {'went to sleep' if phase == 'sleeping' else 'stopped'}", msg, "info")


def spawn(sid: str, coro) -> None:
    t = _tasks.get(sid)
    if t and not t.done():
        t.cancel()
    _tasks[sid] = asyncio.create_task(coro)


def rcmd(s: dict, cmd: str) -> str:
    try:
        host = dockerctl.addr(s["id"])
    except Exception as e:
        raise rcon.RconError(str(e)) from e
    return rcon.command(host, s["rcon_password"], cmd)


async def arcmd(s: dict, cmd: str) -> str:
    return await asyncio.to_thread(rcmd, s, cmd)


# ------------------------------------------------------------------ create
async def create(req: dict) -> dict:
    kind = req["kind"]
    sid = state.unique_id(req.get("name") or req.get("title") or kind)
    s = {
        "id": sid, "name": req.get("name") or sid, "kind": kind,
        "mc_version": req.get("mc_version", ""), "loader": kind if kind not in perf.MODPACK_KINDS else "",
        "pack": None, "rcon_password": secrets.token_urlsafe(18),
        "hostname": router.hostname(sid), "created": time.time(),
        "first_boot": True, "auto_sleep_min": config.DEFAULT_SLEEP_MIN, "extra_env": {},
        "seed": (req.get("seed") or "").strip(),
        "pregen": {"spawn_radius": config.SPAWN_PREGEN_RADIUS, "full_radius": config.FULL_PREGEN_RADIUS,
                   "stage": "pending", "paused": False, "mspt": None},
    }
    if kind == "modrinth":
        info = await catalog.modrinth_pack_version(req["project"], req.get("version_id"))
        s["pack"] = {"project": req["project"], "title": req.get("title"), "version_id": info["id"],
                     "version_name": info["name"]}
        s["mc_version"], s["loader"] = info["mc_version"], info["loader"]
    elif kind == "curseforge":
        info = await catalog.curseforge_pack_info(int(req["project"]))
        s["pack"] = {"id": int(req["project"]), "slug": info["slug"], "title": info["title"],
                     "file_id": info.get("file_id"), "version_name": info.get("version_name")}
        s["mc_version"], s["loader"] = info["mc_version"], info["loader"]
    elif kind == "ftb":
        info = await catalog.ftb_pack_info(int(req["project"]))
        s["pack"] = {"id": int(req["project"]), "title": info["title"], "version_id": info["version_id"],
                     "version_name": info.get("version_name")}
        s["mc_version"], s["loader"] = info["mc_version"], info["loader"]
    if kind == "vanilla":
        s["loader"] = "vanilla"
    s["java_tag"] = req.get("java_tag") or perf.java_tag(s["mc_version"])
    init, soft, mx = perf.memory_plan(kind)
    s["memory"] = {"init_gb": init, "soft_gb": soft, "max_gb": mx, "auto": True}
    s["home_groups"] = perf.home_groups(kind)
    s["phase"] = "queued"
    state.put(s)
    spawn(sid, provision(sid))
    return s


# ------------------------------------------------------------------ start/stop
def _worker_threads(sid: str) -> int:
    return len(topology.allocator().cpus_for(sid))


def _ram_check(s: dict) -> str | None:
    used = sum(dockerctl.mem_usage_bytes(x) for x in state.servers() if x != s["id"]) / 2**30
    need = s["memory"]["init_gb"] + 1
    if used + need > config.ram_pool_gb():
        return f"Minecraft RAM pool is full ({used:.1f}/{config.ram_pool_gb()} GB in use). Stop a server first."
    return None


PREGEN_ACTIVE = ("pending", "spawn", "background")


def ensure_rcon(s: dict) -> bool:
    """SpawnPoint talks to every server over RCON, so make sure server.properties has it on
    with our password. A pack's own server.properties (or one edited by hand) can turn it
    off, and then the server starts but SpawnPoint can't see it. True if anything changed."""
    sid = s["id"]
    if not os.path.exists(os.path.join(dockerctl.server_dir(sid), "server.properties")):
        return False                                  # the image writes it, with RCON, on the first start
    props = dict(read_properties(sid))
    want = {"enable-rcon": "true", "rcon.password": s["rcon_password"], "rcon.port": "25575"}
    change = {k: v for k, v in want.items() if props.get(k, "").strip() != v}
    if change:
        log.info("[%s] turning RCON back on in server.properties (%s)", sid, ", ".join(sorted(change)))
        write_properties(sid, change)
    return bool(change)


def apply_empty_pause(s: dict) -> None:
    """Since 1.21.2 the game stops ticking when nobody's online (pause-when-empty-seconds),
    which also stops pre-generation. Turn it off while pre-gen runs and put the server's
    own value back once it's done. (SpawnPoint's auto-sleep still stops idle servers.)"""
    sid = s["id"]
    props = dict(read_properties(sid))
    cur = props.get("pause-when-empty-seconds")
    active = (s.get("pregen") or {}).get("stage") in PREGEN_ACTIVE
    if active and cur is not None and cur.strip() != "0":
        state.update(sid, empty_pause_restore=cur.strip())
        write_properties(sid, {"pause-when-empty-seconds": "0"})
    elif not active and s.get("empty_pause_restore") is not None:
        if cur is not None and cur.strip() == "0":
            write_properties(sid, {"pause-when-empty-seconds": s["empty_pause_restore"]})
        state.update(sid, empty_pause_restore=None)


async def _boot(s: dict, burst: bool = False) -> None:
    """(Re)create and start the container on its home cores (+ burst cores if lent)."""
    from . import realip
    state.update(s["id"], restart_pending=False)
    if s.get("first_boot") and (s.get("pregen") or {}).get("stage") in PREGEN_ACTIVE:
        state.update(s["id"], empty_pause_restore="60")      # the game's default, once pre-gen is done
        s = state.get(s["id"])
    if not s.get("first_boot"):
        await asyncio.to_thread(ensure_rcon, s)
        await asyncio.to_thread(apply_empty_pause, s)
        # Container is stopped here, so it's safe to (re)apply the real-IP setup.
        from . import packs
        s = await packs.pin_curseforge(s)
        s = await reconcile_install(s)
        s = refresh_memory_plan(s)
        await ensure_pack_helpers(s)
        await realip.ensure(state.get(s["id"]))
        s = state.get(s["id"])
    alloc = topology.allocator()
    alloc.assign_home(s["id"], s["home_groups"])
    if burst:
        alloc.lend_burst(s["id"], config.BURST_GROUPS, config.MAX_BURST_SERVERS)
    cpus = alloc.cpus_for(s["id"])
    # Size Java's world-gen worker pool from the server's own cores: borrowed start-up and
    # pre-gen cores go back later, and a pool sized for them would crowd the main thread.
    own = alloc.cpus_for(s["id"], include_burst=False)
    await asyncio.to_thread(dockerctl.replace_container, s, cpus, max(2, len(own) - 2))
    dockerctl.reset_hub(s["id"]).ensure_running()


async def _wait_ready(s: dict, timeout: float) -> bool:
    """Wait for the server's "Done" line, but give up straight away if it crashes."""
    import time as _t
    h = dockerctl.hub(s["id"])
    deadline = _t.time() + timeout
    while True:
        line = await asyncio.to_thread(h.wait_for, ['Done (', "For help, type"], 0, 5)
        if line:
            break
        if await asyncio.to_thread(dockerctl.status, s["id"]) != "running":
            return False  # exited: crash or bad config
        if _t.time() > deadline:
            return False
    for _ in range(30):  # RCON comes up a moment after "Done"
        try:
            await arcmd(s, "list")
            return True
        except rcon.RconError:
            await asyncio.sleep(2)
    return False


async def boot_ready(sid: str, timeout: float, burst: bool = True, attempts: int = 6) -> bool:
    """Boot and wait until the server is ready. If it crashes because of client-only
    mods (common in modpacks), disable them and try again.

    Boots borrow spare core groups when there are any (burst): loading a modpack is spread
    over many threads, so a 175-mod pack starts several times faster on 16 threads than
    on its own 4-8. go_online gives them back once the server has settled."""
    from . import clientmods, javafix
    tried_java: set[str] = set()
    rcon_fixed = False
    for _ in range(attempts):
        s = state.get(sid)
        t0 = time.time()
        await _boot(s, burst=burst)
        if await _wait_ready(s, timeout):
            took = round(time.time() - t0)
            n = len(topology.allocator().cpus_for(sid))
            state.update(sid, last_start={"seconds": took, "threads": n, "at": time.time()})
            log.info("[%s] started in %ss on %s threads", sid, took, n)
            return True
        lines = await asyncio.to_thread(clientmods.crash_lines, sid)
        if any("Done (" in l for l in lines) and await asyncio.to_thread(ensure_rcon, state.get(sid)):
            # It started, but its console was switched off. Fixed in the file; if something
            # (a pack installer) switches it off again at the next start, have the image set
            # it on every start instead.
            if rcon_fixed:
                state.update(sid, props_from_env=True)
            rcon_fixed = True
            set_phase(sid, state.get(sid).get("phase", "starting"), "The server's console (RCON) was off: turned it on and retrying")
            continue
        fixed = await asyncio.to_thread(clientmods.autofix, s, lines)
        if fixed:
            names = ", ".join(f["name"] for f in fixed)
            set_phase(sid, state.get(sid).get("phase", "starting"),
                      f"Disabled client-only mod(s) {names} and retrying")
            continue
        new_java = javafix.suggest(lines, s.get("java_tag", ""))
        if new_java and new_java not in tried_java:
            tried_java.add(new_java)
            state.update(sid, java_tag=new_java)
            set_phase(sid, state.get(sid).get("phase", "starting"),
                      f"This server needs {new_java.replace('java', 'Java ')}: switched and retrying")
            continue
        return False
    return False


async def _pin(s: dict) -> None:
    home = topology.allocator().cpus_for(s["id"], include_burst=False)
    await asyncio.sleep(5)
    try:
        r = await asyncio.to_thread(dockerctl.pin_threads, s["id"], home)
        state.update(s["id"], pinning=r)
    except Exception as e:
        log.debug("pinning failed: %s", e)


def install_first(s: dict) -> bool:
    """Modded servers and modpacks are installed without starting the game, then tuned,
    then started once: a big pack's first start takes minutes, and starting it twice
    (once to install, once after adding helpers) doubled that. Paper and vanilla start
    in seconds and only write some of their config on a real start, so they keep the
    install-by-starting path."""
    return s.get("kind") not in ("paper", "purpur", "vanilla") and not s.get("imported")


async def install_only(sid: str, timeout: float) -> bool:
    """Run the image's setup (download loader, pack and mods) and wait for it to exit."""
    import time as _t
    state.update(sid, setup_only=True)
    try:
        await _boot(state.get(sid), burst=True)            # downloads and installers use the spare cores too
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            await asyncio.sleep(3)
            if await asyncio.to_thread(dockerctl.status, sid) != "running":
                info = await asyncio.to_thread(dockerctl.exit_info, sid) or {}
                return info.get("code") == 0
        return False
    finally:
        state.update(sid, setup_only=False)


async def provision(sid: str) -> None:
    from . import pregen
    s = state.get(sid)
    try:
        set_phase(sid, "installing", "Downloading server / modpack (big packs can take a while)")
        if install_first(s):
            if not await install_only(sid, config.INSTALL_TIMEOUT_S):
                await start_failed(sid, "The download or install didn't finish. Check the console.")
                return
            set_phase(sid, "preparing", "Adding performance helpers and tuning")
        else:
            if not await boot_ready(sid, config.INSTALL_TIMEOUT_S):
                await start_failed(sid, "Server didn't finish starting. Check the console.")
                return
            set_phase(sid, "preparing", "Adding performance helpers and tuning")
            await asyncio.to_thread(dockerctl.get_container(sid).stop, timeout=120)
        await prepare(s)
        s = state.update(sid, first_boot=False)
        await pregen.run(sid, fresh=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("provision failed")
        set_phase(sid, "error", str(e))


def detect_loader_version(sdir: str) -> tuple[str, str]:
    libs = os.path.join(sdir, "libraries")
    loader = ""
    if os.path.isdir(os.path.join(libs, "net", "neoforged")):
        loader = "neoforge"
    elif os.path.isdir(os.path.join(libs, "net", "minecraftforge")):
        loader = "forge"
    elif os.path.isdir(os.path.join(libs, "net", "fabricmc")) or glob.glob(os.path.join(sdir, "fabric-server*.jar")):
        loader = "fabric"
    elif os.path.isdir(os.path.join(libs, "org", "quiltmc")):
        loader = "quilt"
    ver = ""
    mcdir = os.path.join(libs, "net", "minecraft", "server")
    if os.path.isdir(mcdir):
        for d in sorted(os.listdir(mcdir)):
            m = re.match(r"(\d+\.\d+(?:\.\d+)?)", d)
            if m:
                ver = m.group(1)
    return loader, ver


async def prepare(s: dict) -> None:
    sid, sdir = s["id"], dockerctl.server_dir(s["id"])
    if not s.get("loader") or not s.get("mc_version"):
        loader, ver = detect_loader_version(sdir)
        s = state.update(sid, loader=s.get("loader") or loader, mc_version=s.get("mc_version") or ver)
    s = await reconcile_install(s)
    loader = s["loader"]
    # 1. helper mods/plugins
    added = []
    pack_ids = await asyncio.to_thread(pack_mod_ids, s) if is_managed(s) else None
    for slug in HELPERS.get(loader, []):
        if has_mod(state.get(sid), slug, pack_ids):
            continue
        try:
            r = await install_mod(state.get(sid), slug)
            added.append(r["filename"])
        except Exception as e:
            log.info("[%s] skipped helper %s: %s", sid, slug, e)
    # 2. memory from real mod count
    n = len(mod_files(s))
    if s["memory"].get("auto"):
        init, soft, mx = perf.memory_plan(s["kind"], n)
        s = state.update(sid, memory={"init_gb": init, "soft_gb": soft, "max_gb": mx, "auto": True},
                         home_groups=perf.home_groups(s["kind"], n))
    # 3. Paper: let chunk generation use more worker threads
    if loader in ("paper", "purpur"):
        tune_paper(sdir, worker_threads=4 + 4 * config.BURST_GROUPS)
    state.update(sid, helpers_added=added, mod_count=len(mod_files(s)))


def tune_paper(sdir: str, worker_threads: int) -> None:
    import yaml
    p = os.path.join(sdir, "config", "paper-global.yml")
    if not os.path.exists(p):
        return
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    cs = data.setdefault("chunk-system", {})
    cs["worker-threads"] = worker_threads
    with open(p, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)


async def start(sid: str) -> dict:
    from . import pregen
    s = state.get(sid)
    if err := _ram_check(s):
        raise ValueError(err)
    if s.get("first_boot"):
        # Its first install never finished (e.g. a crash): run the whole install again.
        spawn(sid, provision(sid))
        return s
    resume = s["pregen"]["stage"] in ("spawn", "background", "pending")
    set_phase(sid, "starting", "")

    async def go():
        try:
            if resume:
                await pregen.run(sid, fresh=False)
                return
            # a modpack update downloads the new version first, which can take a while
            if await boot_ready(sid, config.INSTALL_TIMEOUT_S if state.get(sid).get("pack_pending") else 600):
                state.update(sid, pack_pending=False)
                await go_online(sid)
            else:
                await start_failed(sid, "Didn't start in time. Check the console.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("start failed")
            set_phase(sid, "error", str(e))
    spawn(sid, go())
    return s


BOOST_SETTLE_S = 90       # keep the start-up cores this long after "Done" (JIT warm-up, first ticks)


async def _end_boost(sid: str) -> None:
    """Give back the cores borrowed for start-up, unless pre-generation is using them."""
    await asyncio.sleep(BOOST_SETTLE_S)
    s = state.get(sid)
    alloc = topology.allocator()
    if not s or not alloc.burst.get(sid) or s.get("pregen", {}).get("stage") in ("spawn", "background"):
        return
    alloc.return_burst(sid)
    try:
        await asyncio.to_thread(dockerctl.set_cpuset, sid, alloc.cpus_for(sid))
    except Exception as e:
        log.warning("[%s] couldn't return start-up cores: %s", sid, e)


async def go_online(sid: str) -> None:
    s = state.get(sid)
    asyncio.create_task(_pin(s))
    asyncio.create_task(_end_boost(sid))
    from . import sharedlist
    if sharedlist.settings(s)["enabled"]:
        asyncio.create_task(sharedlist.sync(sid))          # catch up on shared-list changes made while it was off
    from . import proxy
    try:
        await proxy.verify_real_ip(sid)
    except Exception as e:
        log.warning("[%s] real-IP check failed: %s", sid, e)
    set_phase(sid, "online", "")


async def stop(sid: str) -> None:
    s = state.get(sid)
    t = _tasks.get(sid)
    if t and not t.done():
        t.cancel()
    set_phase(sid, "stopping", "")
    try:
        rcon.drop(dockerctl.addr(sid))
    except Exception:
        pass
    c = dockerctl.get_container(sid)
    if c and c.status == "running":
        await asyncio.to_thread(c.stop, timeout=120)
    topology.allocator().release(sid)
    set_phase(sid, "stopped", "")


async def kill(sid: str) -> None:
    """Force stop: end the server immediately, without saving. For a server that's
    stuck, or one still installing. Unsaved progress since the last autosave is lost."""
    t = _tasks.get(sid)
    if t and not t.done():
        t.cancel()
    try:
        rcon.drop(dockerctl.addr(sid))
    except Exception:
        pass
    c = dockerctl.get_container(sid)
    if c:
        try:
            await asyncio.to_thread(c.kill)
        except Exception as e:  # already stopped
            log.info("[%s] kill: %s", sid, e)
    topology.allocator().release(sid)
    s = state.get(sid)
    if s["pregen"].get("stage") in ("spawn", "background"):
        s["pregen"]["paused"] = False
    set_phase(sid, "stopped", "Force stopped")


def _download_detail(s: dict) -> str:
    mdir = mods_dir(s)
    n = mb = 0
    if os.path.isdir(mdir):
        for f in os.listdir(mdir):
            if f.endswith(".jar"):
                n += 1
                try:
                    mb += os.path.getsize(os.path.join(mdir, f))
                except OSError:
                    pass
    what = "modpack" if s.get("pack") else "server"
    return f"Downloading {what}" + (f": {n} mods ({mb / 2**20:.0f} MB)" if n else "")


def install_progress(s: dict) -> dict | None:
    """Step-by-step progress for a server that's being set up, for the cards."""
    phase = s.get("phase")
    if install_first(s):
        steps = ["Downloading", "Tuning", "First start", "Generating spawn"]
        if phase == "installing":
            return {"step": 1, "of": 4, "label": steps[0], "detail": _download_detail(s), "percent": None}
        if phase == "preparing":
            return {"step": 2, "of": 4, "label": steps[1], "detail": "Adding performance helpers", "percent": None}
        if phase == "pregen-spawn" and (s.get("pregen") or {}).get("stage") in ("pending", None):
            n = len([f for f in mod_files(s) if f.endswith(".jar")])
            return {"step": 3, "of": 4, "label": steps[2],
                    "detail": f"Loading {n} mods for the first time" if n else "Starting up for the first time", "percent": None}
    steps = ["Downloading", "First start", "Tuning", "Generating spawn"]
    if phase == "installing":
        h = dockerctl.hub(s["id"])
        started = any("Starting the Minecraft server" in l or "Starting minecraft server" in l
                      for _, l in list(h.lines))
        mdir = mods_dir(s)
        n = mb = 0
        if os.path.isdir(mdir):
            for f in os.listdir(mdir):
                if f.endswith(".jar"):
                    n += 1
                    try:
                        mb += os.path.getsize(os.path.join(mdir, f))
                    except OSError:
                        pass
        if started:
            return {"step": 2, "of": 4, "label": steps[1],
                    "detail": f"Loading {n} mods for the first time" if n else "Starting up for the first time",
                    "percent": None}
        what = "modpack" if s.get("pack") else "server"
        detail = f"Downloading {what}" + (f": {n} mods ({mb / 2**20:.0f} MB)" if n else "")
        return {"step": 1, "of": 4, "label": steps[0], "detail": detail, "percent": None}
    if phase == "importing":
        pct = s.get("import_progress") or 0
        return {"step": 1, "of": 3, "label": "Unpacking", "detail": f"{pct:.0f}% unpacked", "percent": pct}
    if phase == "installing" and s.get("imported"):
        return {"step": 2, "of": 3, "label": "Installing", "detail": s.get("phase_msg") or "", "percent": None}
    if phase == "queued":
        return {"step": 1, "of": 4, "label": "Queued", "detail": "Waiting to start", "percent": None}
    if phase == "preparing":
        return {"step": 3, "of": 4, "label": steps[2], "detail": "Adding performance helpers", "percent": None}
    if phase == "pregen-spawn":
        p = s.get("pregen", {})
        pct = p.get("percent") or 0
        return {"step": 4, "of": 4, "label": steps[3],
                "detail": f"{p.get('spawn_radius', '')}-block area around spawn: {pct:.0f}%", "percent": pct}
    return None


async def restart(sid: str) -> None:
    await stop(sid)
    await start(sid)


async def delete(sid: str, remove_files: bool) -> None:
    await stop(sid)
    c = dockerctl.get_container(sid)
    if c:
        await asyncio.to_thread(c.remove)
    if remove_files:
        await asyncio.to_thread(shutil.rmtree, dockerctl.server_dir(sid), True)
    state.delete(sid)


# ------------------------------------------------------------------ properties
def read_properties(sid: str) -> list[tuple[str, str]]:
    p = os.path.join(dockerctl.server_dir(sid), "server.properties")
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out.append((k, v))
    return out


def write_properties(sid: str, values: dict[str, str]) -> None:
    p = os.path.join(dockerctl.server_dir(sid), "server.properties")
    lines, seen = [], set()
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                raw = line.rstrip("\n")
                if "=" in raw and not raw.startswith("#"):
                    k = raw.split("=", 1)[0]
                    if k in values:
                        raw = f"{k}={values[k]}"
                        seen.add(k)
                lines.append(raw)
    for k, v in values.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------ crashes
CRASH_LIMIT = 3            # restarts allowed ...
CRASH_WINDOW_S = 15 * 60   # ... within this long, before giving up
_CRASH_MARKERS = ("This crash report has been saved", "Preparing crash report", "A single server tick took",
                  "Considering it to be crashed", "Exception in server tick loop", "OutOfMemoryError")


def _crash_reason(sid: str, info: dict, lines: list[str]) -> str | None:
    """Why the server went down if it wasn't asked to, or None for a clean stop
    (someone typed `stop` in the console)."""
    if info.get("oom"):
        return "ran out of memory (killed by the system)"
    hit = next((l for l in reversed(lines) if any(m in l for m in _CRASH_MARKERS)), None)
    if hit:
        if "A single server tick took" in hit or "Considering it to be crashed" in hit:
            return "froze (the watchdog stopped it)"
        if "OutOfMemoryError" in hit:
            return "ran out of memory (Java heap)"
        return "crashed (a crash report was saved)"
    if info.get("code") not in (0, None):
        return f"exited with code {info['code']}"
    return None


def explain_crash(s: dict, lines: list[str]) -> dict:
    """crashinfo.explain for this server, from its last start onwards."""
    from . import crashinfo
    try:
        return crashinfo.explain(s["id"], lines, since=(s.get("last_start") or {}).get("at"), mods_dir=mods_dir(s))
    except Exception as e:                                   # never let the explainer hide the crash itself
        log.debug("crash explain failed: %s", e)
        return {}


def _plain(ex: dict) -> str:
    if not ex or not ex.get("title"):
        return ""
    who = f" (mod: {ex['mod']}" + (f", file {ex['jar']}" if ex.get("jar") else "") + ")" if ex.get("mod") else ""
    return f"{ex['title']}{who}. {ex.get('detail', '')} {ex.get('hint', '')}".strip()


async def start_failed(sid: str, message: str) -> None:
    """A start that didn't finish: say why, if the log or a crash report shows it."""
    from . import activity, alerts
    s = state.get(sid)
    lines = await asyncio.to_thread(dockerctl.recent_logs, sid, 4000)
    ex = await asyncio.to_thread(explain_crash, s, lines)
    if ex.get("title"):
        state.update(sid, last_crash={"at": time.time(), "reason": "didn't start", "explain": ex, "line": ""})
        message = f"{message.rstrip('.')}: {ex['title'].lower()}" + (f" ({ex['mod']})" if ex.get("mod") else "") + \
            ". Details are above the console."
        activity.record("SpawnPoint", "system", "", "server.crash", sid, _plain(ex)[:500], False)
        alerts.send("crash_gave_up", s.get("name", sid), f"{s.get('name', sid)} didn't start", _plain(ex), "bad")
    set_phase(sid, "error", message)


async def check_crash(sid: str) -> None:
    """Called by the stats loop for servers marked online whose container isn't running.
    SpawnPoint's own stops change the phase first, so this only sees exits nobody asked for."""
    from . import activity
    s = state.get(sid)
    if not s or s.get("phase") not in ("online",) or (sid in _tasks and not _tasks[sid].done()):
        return
    info = await asyncio.to_thread(dockerctl.exit_info, sid)
    if info is None:
        return
    lines = await asyncio.to_thread(dockerctl.recent_logs, sid, 300)
    topology.allocator().release(sid)
    reason = _crash_reason(sid, info, lines)
    if reason is None:
        set_phase(sid, "stopped", "Stopped from the console")
        activity.record("SpawnPoint", "system", "", "server.stop", sid, "Stopped with the stop command", True)
        return
    last = next((l.strip() for l in reversed(lines) if l.strip() and ("ERROR" in l or "Exception" in l or "crash" in l.lower())), "")
    now = time.time()
    recent = [t for t in s.get("crashes", []) if now - t < CRASH_WINDOW_S] + [now]
    ex = await asyncio.to_thread(explain_crash, s, lines)
    state.update(sid, crashes=recent, last_crash={"at": now, "reason": reason, "line": last[:300], "explain": ex})
    log.warning("[%s] %s: %s", sid, reason, last[:200])
    plain = _plain(ex)
    activity.record("SpawnPoint", "system", "", "server.crash", sid, (f"{reason}. {plain}" if plain else f"{reason}. {last}")[:500], False)
    from . import alerts
    name = s.get("name", sid)
    detail = f"Reason: {reason}." + (f"\n{plain}" if plain else "") + (f"\nLast error: `{last[:300]}`" if last and not plain else "")
    if not s.get("auto_restart", True):
        set_phase(sid, "error", f"The server {reason}. Automatic restart is off. Check the console.")
        alerts.send("crash_gave_up", name, f"{name} crashed", detail + "\nAutomatic restart is off, so it's stopped.", "bad")
        return
    if len(recent) > CRASH_LIMIT:
        set_phase(sid, "error", f"The server {reason}, and crashed {len(recent)} times in 15 minutes, so it wasn't "
                  "restarted again. Check the console, then start it.")
        alerts.send("crash_gave_up", name, f"{name} keeps crashing",
                    detail + f"\nIt crashed {len(recent)} times in 15 minutes, so SpawnPoint stopped restarting it.", "bad")
        return
    alerts.send("crash", name, f"{name} crashed and is restarting", detail, "warn")
    if _ram_check(s):
        set_phase(sid, "error", f"The server {reason}. Not restarted: the RAM pool is full.")
        return
    await start(sid)
    set_phase(sid, "starting", f"Restarting: the server {reason}")


# ------------------------------------------------------------------ shutdown / startup
async def shutdown_all() -> None:
    """Isolated mode: SpawnPoint's private engine goes down with it, so save and stop
    every running server cleanly and remember which ones to bring back."""
    if not config.ISOLATED:
        return
    running = [sid for sid in list(state.servers()) if dockerctl.status(sid) == "running"]
    for sid in running:
        state.update(sid, resume_on_boot=True)

    async def one(sid):
        try:
            await arcmd(state.get(sid), "save-all flush")
        except Exception:
            pass
        c = dockerctl.get_container(sid)
        if c:
            await asyncio.to_thread(c.stop, timeout=120)
    await asyncio.gather(*(one(sid) for sid in running), return_exceptions=True)
    log.info("stopped %d server(s) for shutdown", len(running))


async def resume_all() -> None:
    """On panel restart: re-learn core assignments, re-add routes, resume pre-gen."""
    from . import pregen
    for sid, s in list(state.servers().items()):
        st = await asyncio.to_thread(dockerctl.status, sid)
        if st != "running" and s.get("phase") == "importing":
            set_phase(sid, "error", "The import was interrupted by a restart. Delete it and import again.")
            continue
        if st != "running":
            was_up = s.pop("resume_on_boot", False) or s.get("phase") in (
                "online", "pregen-spawn", "starting", "installing", "preparing")
            state.save()
            if was_up and s.get("phase") != "error":
                # Unraid reboot / SpawnPoint update: bring back what was running.
                set_phase(sid, "stopped", "")
                try:
                    if s.get("phase") == "installing" or s.get("first_boot"):
                        spawn(sid, provision(sid))
                    else:
                        await start(sid)
                except Exception as e:
                    log.warning("[%s] auto-resume failed: %s", sid, e)
            elif s.get("phase") not in ("stopped", "sleeping", "error", "queued"):
                set_phase(sid, "stopped", "")
            continue
        topology.allocator().assign_home(sid, s["home_groups"])
        dockerctl.hub(sid).ensure_running()
        if s["pregen"]["stage"] == "background":
            set_phase(sid, "online", "Pre-generating the wider world in the background")
            spawn(sid, pregen.background(sid))
        elif s.get("phase") not in ("online",):
            set_phase(sid, "online", "")
