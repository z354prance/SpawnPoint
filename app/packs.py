"""Modpack versions: pinning, update checks, updates (with a backup first) and rollback.

Every modpack server is pinned to one version, so restarting never changes the pack.
Modrinth and FTB always were; CurseForge used to follow the newest file on every start.
"""
import asyncio
import json
import logging
import os
import time

from . import catalog, config, dockerctl, perf, state

log = logging.getLogger("spawnpoint.packs")
CHECK_EVERY_S = 6 * 3600


def pinned_id(s: dict):
    p = s.get("pack") or {}
    return p.get("file_id") if s.get("kind") == "curseforge" else p.get("version_id")


async def latest(s: dict) -> dict | None:
    """The newest version of the server's modpack: {"id", "name", "mc_version", "loader"}."""
    kind, p = s.get("kind"), s.get("pack") or {}
    if kind == "modrinth":
        v = await catalog.modrinth_pack_version(p["project"])
        return {"id": v["id"], "name": v.get("name") or "", "mc_version": v["mc_version"], "loader": v["loader"]}
    if kind == "curseforge":
        if not config.CF_API_KEY:
            return None
        v = await catalog.curseforge_pack_info(int(p["id"]))
        return {"id": v.get("file_id"), "name": v.get("version_name") or "", "mc_version": v["mc_version"], "loader": v["loader"]}
    if kind == "ftb":
        v = await catalog.ftb_pack_info(int(p["id"]))
        return {"id": v.get("version_id"), "name": v.get("version_name") or "", "mc_version": v["mc_version"], "loader": v["loader"]}
    return None


def _installed_cf_file(sid: str) -> int | None:
    """The CurseForge file the server's files came from, read from the installer's manifest."""
    d = dockerctl.server_dir(sid)
    try:
        names = [f for f in os.listdir(d) if f.startswith(".") and "manifest" in f.lower() and f.endswith(".json")]
    except OSError:
        return None
    for f in names:
        try:
            with open(os.path.join(d, f)) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for k in ("modpackFileId", "fileId", "file_id"):
            v = data.get(k) if isinstance(data, dict) else None
            if isinstance(v, int) and v > 0:
                return v
    return None


async def pin_curseforge(s: dict) -> dict:
    """Older CurseForge servers weren't pinned. Pin them to what's installed (or, if that
    can't be read, to the newest file, which is exactly what this start would have installed)."""
    if s.get("kind") != "curseforge" or (s.get("pack") or {}).get("file_id"):
        return s
    fid = await asyncio.to_thread(_installed_cf_file, s["id"])
    name = ""
    if not fid:
        try:
            v = await latest(s)
        except Exception as e:
            log.info("[%s] couldn't look up the CurseForge pack to pin it: %s", s["id"], e)
            return s
        if not v or not v.get("id"):
            return s
        fid, name = v["id"], v["name"]
    pack = {**s["pack"], "file_id": int(fid)}
    if name:
        pack["version_name"] = name
    log.info("[%s] pinned the CurseForge pack to file %s", s["id"], fid)
    state.update(s["id"], pack=pack)
    return state.get(s["id"])


async def check(sid: str) -> dict | None:
    """Look for a newer version and remember it on the server (pack_update)."""
    s = state.get(sid)
    if not s or not s.get("pack"):
        return None
    try:
        v = await latest(s)
    except Exception as e:
        log.info("[%s] update check failed: %s", sid, e)
        return None
    found = v if v and v.get("id") and str(v["id"]) != str(pinned_id(s)) else None
    known = (s.get("pack_update") or {}).get("id")
    state.update(sid, pack_update=found, pack_checked=time.time())
    if found and str(found["id"]) != str(known):
        from . import alerts
        name = s.get("name", sid)
        alerts.send("pack_update", name, f"Update available for {name}",
                    f"{(s.get('pack') or {}).get('title', 'The modpack')} {found.get('name', '')} is out. "
                    "Update it from the server's Settings tab (it backs up first).", "info")
    return found


async def loop() -> None:
    await asyncio.sleep(120)
    while True:
        for sid, s in list(state.servers().items()):
            if s.get("pack") and not s.get("first_boot") and time.time() - (s.get("pack_checked") or 0) > CHECK_EVERY_S:
                await check(sid)
                await asyncio.sleep(2)
        await asyncio.sleep(600)


def _apply(s: dict, v: dict) -> dict:
    pack = dict(s["pack"])
    if s["kind"] == "curseforge":
        pack["file_id"] = int(v["id"])
    else:
        pack["version_id"] = v["id"]
    pack["version_name"] = v.get("name") or pack.get("version_name")
    changes = {"pack": pack, "pack_update": None, "pack_pending": True}
    if v.get("mc_version") and v["mc_version"] != s.get("mc_version"):
        changes["mc_version"] = v["mc_version"]
        changes["java_tag"] = perf.java_tag(v["mc_version"])
    return changes


async def update(sid: str) -> dict:
    """Back up, switch to the newest version, and restart if it was running.
    The backup and the old version are kept so it can be rolled back."""
    from . import backups, servers
    s = state.get(sid)
    v = s.get("pack_update") or await check(sid)
    if not v:
        raise ValueError("Already on the newest version")
    was_running = dockerctl.status(sid) == "running"
    snap = await backups.create(sid, "before-update")
    prev = {"pack": s["pack"], "mc_version": s.get("mc_version"), "java_tag": s.get("java_tag"),
            "backup": snap.get("id"), "at": time.time(), "to": v.get("name")}
    state.update(sid, pack_prev=prev, **_apply(s, v))
    if was_running:
        await servers.restart(sid)
    return {"version": v.get("name"), "restarted": was_running}


async def rollback(sid: str) -> dict:
    """Put back the world, mods and pack version from just before the last update."""
    from . import backups, servers
    s = state.get(sid)
    prev = s.get("pack_prev")
    if not prev or not prev.get("backup"):
        raise ValueError("There's no update to roll back")
    was_running = dockerctl.status(sid) == "running"
    if was_running:
        await servers.stop(sid)
    await backups.restore(sid, prev["backup"])
    state.update(sid, pack=prev["pack"], mc_version=prev["mc_version"], java_tag=prev["java_tag"],
                 pack_prev=None, pack_pending=True, pack_update=None, pack_checked=0)
    if was_running:
        await servers.start(sid)
    return {"restarted": was_running}
