"""Import an existing server from an uploaded .zip / .tar.gz and deploy it.

Steps: unpack safely (no paths escaping the folder, strip a single wrapping folder,
check disk space) -> work out the server type and versions from its own files ->
take over the few settings SpawnPoint needs (port, RCON) -> run it through the
normal start flow (Java/RAM/cores, helpers, real IPs, client-mod and Java fixes),
with background pre-generation filling in only chunks that don't exist yet.
"""
import asyncio
import glob
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import time
import zipfile

from . import config, dockerctl, perf, router, servers, state

log = logging.getLogger("spawnpoint.importer")
UPLOAD_DIR = os.path.join(config.STATE_DIR, "uploads")
KINDS = ("forge", "neoforge", "fabric", "quilt", "paper", "purpur", "vanilla")


class ImportError_(Exception):
    pass


# ------------------------------------------------------------------ unpacking
def _members(path: str) -> tuple[str, list[tuple[str, int, bool]]]:
    """('zip'|'tar', [(name, size, is_dir)])"""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            return "zip", [(i.filename, i.file_size, i.is_dir()) for i in z.infolist()]
    try:
        with tarfile.open(path) as t:
            return "tar", [(m.name, m.size, m.isdir()) for m in t.getmembers() if m.isfile() or m.isdir()]
    except tarfile.TarError:
        raise ImportError_("That file isn't a .zip or .tar.gz archive.")


FRESH_REGIONS = 9            # a first start makes about 4 region files; more means people have played


def _norm(name: str) -> str:
    """Archive member name with backslashes fixed and a leading './' or '/' removed.
    (Not lstrip('./'): that would turn '.fabric' into 'fabric' and hide '../'.)"""
    n = name.replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    return n.lstrip("/")


def _junk(n: str) -> bool:
    return n.startswith("__MACOSX/") or "/__MACOSX/" in n or n.endswith(".DS_Store")


def _common_root(names: list[str]) -> str:
    """If everything sits inside one top folder (common when zipping a folder), return it."""
    real = [_norm(n) for n in names if _norm(n).strip("/") and not _junk(_norm(n))]
    tops = {n.split("/", 1)[0] for n in real}
    if len(tops) == 1 and any("/" in n.rstrip("/") for n in real):
        return tops.pop() + "/"
    return ""


def _safe_target(dest: str, name: str, strip: str) -> str | None:
    n = _norm(name)
    if ".." in n.split("/"):
        raise ImportError_(f"The archive contains an unsafe path: {name}")
    if strip and n.startswith(strip):
        n = n[len(strip):]
    if not n or _junk(n):
        return None
    target = os.path.realpath(os.path.join(dest, n))
    root = os.path.realpath(dest)
    if target != root and not target.startswith(root + os.sep):
        raise ImportError_(f"The archive contains an unsafe path: {name}")
    return target


def extract(path: str, dest: str, progress=lambda done, total: None) -> None:
    kind, members = _members(path)
    total = sum(sz for n, sz, d in members if not d and not _junk(_norm(n))) or 1
    os.makedirs(dest, exist_ok=True)
    free = shutil.disk_usage(dest).free
    if total * 1.1 > free:
        raise ImportError_(f"Not enough disk space: needs {total / 2**30:.1f} GB, "
                           f"{free / 2**30:.1f} GB free.")
    strip = _common_root([n for n, _, _ in members])
    os.makedirs(dest, exist_ok=True)
    done = 0
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                target = _safe_target(dest, info.filename, strip)
                if target is None:
                    continue
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                done += info.file_size
                progress(done, total)
    else:
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if not (m.isfile() or m.isdir()):
                    continue  # skip links and devices
                target = _safe_target(dest, m.name, strip)
                if target is None:
                    continue
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = t.extractfile(m)
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                done += m.size
                progress(done, total)


# ------------------------------------------------------------------ detection
def _dirs(path: str) -> list[str]:
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def _jar_version_json(jar: str) -> str:
    try:
        with zipfile.ZipFile(jar) as z:
            if "version.json" in z.namelist():
                return json.loads(z.read("version.json")).get("id", "")
    except (OSError, zipfile.BadZipFile, ValueError):
        pass
    return ""


def looks_like_mc(v: str) -> bool:
    """1.x.y, or the year-based versions from 26.1 on. Not a loader version like 0.20.1."""
    return bool(re.fullmatch(r"1\.\d+(?:\.\d+)?|[2-9]\d\.\d+(?:\.\d+)?", v or ""))


def detect(sdir: str) -> dict:
    """Server type and versions from the server's own files."""
    libs = os.path.join(sdir, "libraries")
    out = {"kind": "", "mc_version": "", "loader_version": ""}
    _, mc = servers.detect_loader_version(sdir)

    neo = os.path.join(libs, "net", "neoforged", "neoforge")
    neo_legacy = os.path.join(libs, "net", "neoforged", "forge")
    forge = os.path.join(libs, "net", "minecraftforge", "forge")
    if _dirs(neo):
        out.update(kind="neoforge", loader_version=_dirs(neo)[-1])
    elif _dirs(neo_legacy):
        v = _dirs(neo_legacy)[-1]
        out.update(kind="neoforge", loader_version=v.split("-", 1)[-1])
        mc = mc or v.split("-", 1)[0]
    elif _dirs(forge):
        v = _dirs(forge)[-1]
        out.update(kind="forge", loader_version=v.split("-", 1)[-1])
        mc = mc or v.split("-", 1)[0]
    elif glob.glob(os.path.join(sdir, "forge-*.jar")):  # old Forge (1.16 and earlier)
        m = re.match(r"forge-(\d+\.\d+(?:\.\d+)?)-(\d+(?:\.\d+)*)", os.path.basename(glob.glob(os.path.join(sdir, "forge-*.jar"))[0]))
        if m:
            out.update(kind="forge", loader_version=m.group(2))
            mc = mc or m.group(1)
    elif _dirs(os.path.join(libs, "org", "quiltmc", "quilt-loader")):
        out.update(kind="quilt", loader_version=_dirs(os.path.join(libs, "org", "quiltmc", "quilt-loader"))[-1])
    elif (_dirs(os.path.join(libs, "net", "fabricmc", "fabric-loader"))
          or glob.glob(os.path.join(sdir, "fabric-server*.jar")) or os.path.isdir(os.path.join(sdir, ".fabric"))):
        lv = _dirs(os.path.join(libs, "net", "fabricmc", "fabric-loader"))
        out.update(kind="fabric", loader_version=lv[-1] if lv else "")
    if out["kind"] in ("fabric", "quilt") and not mc:
        # 26.x has no intermediary mappings any more, so look in several places, and never
        # take the loader's own version (0.20.1) for Minecraft's.
        inter = _dirs(os.path.join(libs, "net", "fabricmc", "intermediary"))
        cands = list(inter)
        for j in glob.glob(os.path.join(sdir, "fabric-server-mc.*-loader*.jar")):
            m = re.search(r"fabric-server-mc\.(.+?)-loader", os.path.basename(j))
            if m:
                cands.append(m.group(1))
        cands += _dirs(os.path.join(sdir, "versions"))          # the game's own bundler unpacks here
        for d in _dirs(os.path.join(sdir, ".fabric", "server")):
            cands += re.findall(r"\d+\.\d+(?:\.\d+)?", d)
        mc = next((c for c in cands if looks_like_mc(c) and c != out["loader_version"]), "")

    if not out["kind"]:
        for name, kind in (("purpur", "purpur"), ("paper", "paper")):
            jars = glob.glob(os.path.join(sdir, f"{name}*.jar"))
            if jars or os.path.exists(os.path.join(sdir, "config", f"{name}-global.yml")) \
                    or (name == "paper" and os.path.exists(os.path.join(sdir, "config", "paper-global.yml"))):
                out["kind"] = kind
                for j in jars:
                    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", os.path.basename(j))
                    if m:
                        mc = mc or m.group(1)
                vh = os.path.join(sdir, "version_history.json")
                if not mc and os.path.exists(vh):
                    m = re.search(r"MC: (\d+\.\d+(?:\.\d+)?)", open(vh).read())
                    mc = m.group(1) if m else ""
                if not mc:
                    vers = _dirs(os.path.join(sdir, "versions"))
                    mc = vers[-1] if vers else ""
                break

    if not out["kind"]:
        jar = next((p for p in [os.path.join(sdir, "server.jar")] + glob.glob(os.path.join(sdir, "minecraft_server*.jar"))
                    if os.path.exists(p)), None)
        if jar:
            out["kind"] = "vanilla"
            mc = mc or _jar_version_json(jar)
            m = re.search(r"minecraft_server\.(\d+\.\d+(?:\.\d+)?)", jar)
            mc = mc or (m.group(1) if m else "")

    if not out["kind"] and os.path.isdir(os.path.join(sdir, "mods")):
        out["kind"] = "unknown-modded"
    out["mc_version"] = mc
    out["has_world"] = any(os.path.exists(os.path.join(sdir, d, "level.dat")) for d in _dirs(sdir))
    # A world that was only started once (a fresh server pack) has a few region files
    # around spawn. Treat it as new, so the spawn area is generated before it opens.
    regions = 0
    for d in _dirs(sdir):
        if not os.path.exists(os.path.join(sdir, d, "level.dat")):
            continue
        for rd in (os.path.join(sdir, d, "region"), os.path.join(sdir, d, "dimensions", "minecraft", "overworld", "region")):
            if os.path.isdir(rd):
                regions = max(regions, sum(1 for f in os.listdir(rd) if f.endswith(".mca")))
    out["world_regions"] = regions
    out["played_world"] = regions > FRESH_REGIONS
    return out


def take_over_properties(sid: str, rcon_password: str) -> None:
    """Keep the server's own settings, but use the port and RCON SpawnPoint expects."""
    servers.write_properties(sid, {
        "server-port": "25565", "enable-rcon": "true", "rcon.port": "25575",
        "rcon.password": rcon_password, "enable-query": "false",
    })


# ------------------------------------------------------------------ flow
async def start_import(upload_path: str, name: str, kind: str = "", mc_version: str = "") -> dict:
    sid = state.unique_id(name or "imported")
    s = {
        "id": sid, "name": name or sid, "kind": "imported", "loader": "", "mc_version": "",
        "loader_version": "", "pack": None, "rcon_password": secrets.token_urlsafe(18),
        "hostname": router.hostname(sid), "created": time.time(), "first_boot": False,
        "auto_sleep_min": config.DEFAULT_SLEEP_MIN, "extra_env": {}, "imported": True, "java_tag": "latest",
        "memory": {"init_gb": 2, "max_gb": 8, "auto": True}, "home_groups": 1,
        "pregen": {"spawn_radius": config.SPAWN_PREGEN_RADIUS, "full_radius": config.FULL_PREGEN_RADIUS,
                   "stage": "pending", "paused": False, "mspt": None},
        "phase": "importing", "phase_msg": "Unpacking", "import_progress": 0.0,
        "import_override": {"kind": kind, "mc_version": mc_version},
    }
    state.put(s)
    servers.spawn(sid, _run(sid, upload_path))
    return s


async def _run(sid: str, upload_path: str) -> None:
    from . import pregen
    sdir = dockerctl.server_dir(sid)
    try:
        last = [0.0]

        def progress(done, total):
            pct = round(done / total * 100, 1)
            if pct - last[0] >= 1 or pct >= 100:
                last[0] = pct
                state.update(sid, import_progress=pct)
        await asyncio.to_thread(extract, upload_path, sdir, progress)
        os.remove(upload_path)

        s = state.get(sid)
        found = await asyncio.to_thread(detect, sdir)
        ov = s.get("import_override") or {}
        kind = ov.get("kind") or found["kind"]
        mc = ov.get("mc_version") or found["mc_version"]
        if kind not in KINDS:
            raise ImportError_("Couldn't tell what kind of server this is. Delete it and import again, "
                               "choosing the type and Minecraft version yourself.")
        if not mc:
            raise ImportError_(f"Found a {kind} server but couldn't tell its Minecraft version. "
                               "Delete it and import again, choosing the version yourself.")
        if kind == "vanilla" and found["kind"] != "vanilla" and os.path.isdir(os.path.join(sdir, "mods")):
            log.info("[%s] importing as vanilla although a mods folder exists", sid)
        loader_version = found["loader_version"] if kind == found["kind"] else ""
        await asyncio.to_thread(take_over_properties, sid, s["rcon_password"])
        n = len([f for f in os.listdir(os.path.join(sdir, "mods")) if f.endswith(".jar")]) \
            if os.path.isdir(os.path.join(sdir, "mods")) else 0
        init, soft, mx = perf.memory_plan(kind, n)
        s = state.update(sid, kind=kind, loader=kind, mc_version=mc, loader_version=loader_version,
                         java_tag=perf.java_tag(mc), memory={"init_gb": init, "soft_gb": soft, "max_gb": mx, "auto": True},
                         home_groups=perf.home_groups(kind, n), mod_count=n, import_progress=100.0,
                         detected=found, phase="installing",
                         phase_msg=f"Installing {kind} {mc}" + (f" ({loader_version})" if loader_version else ""))
        await servers.prepare(s)
        # Explored world: open straight away and only fill in chunks that don't exist yet.
        st = state.get(sid)
        st["pregen"]["stage"] = "background" if found.get("played_world") else "pending"
        state.update(sid, pregen=st["pregen"])
        await pregen.run(sid, fresh=False)
    except asyncio.CancelledError:
        raise
    except ImportError_ as e:
        servers.set_phase(sid, "error", str(e))
    except Exception as e:
        log.exception("import failed")
        servers.set_phase(sid, "error", f"Import failed: {e}")
    finally:
        if os.path.exists(upload_path):
            try:
                os.remove(upload_path)
            except OSError:
                pass
