"""Switching a plain server between vanilla, Fabric, Paper and Purpur.

The world stays: these all read the same world. A backup is made first, the record's type
is changed, the old software's launcher files are cleared out (so the type found in the
files at the next start agrees), and the server starts again if it was running. undo()
puts the backup and the old type back.

Which ways are offered:
  vanilla -> Fabric, Paper, Purpur
  Fabric/Quilt -> vanilla        (mods are kept in the folder; vanilla ignores them)
  Paper <-> Purpur               (same plugins, same configs)
Paper and Purpur keep the Nether and End in their own world folders, so going back to
vanilla from them is done by undoing the switch instead."""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import time

import httpx

from . import backups, dockerctl, servers, state

log = logging.getLogger("spawnpoint.software")
NAMES = {"vanilla": "Vanilla", "fabric": "Fabric", "quilt": "Quilt", "paper": "Paper", "purpur": "Purpur"}
TARGETS = {"vanilla": ("fabric", "paper", "purpur"), "fabric": ("vanilla",), "quilt": ("vanilla",),
           "paper": ("purpur",), "purpur": ("paper",)}
_transport = None                                       # tests swap in a fake network
_tasks: dict[str, asyncio.Task] = {}


def current(s: dict) -> str:
    return (s.get("kind") if s.get("kind") in TARGETS else s.get("loader")) or ""


def choices(s: dict) -> list[str]:
    """What this server can switch to (nothing for modpacks or servers without a set version)."""
    if servers.is_managed(s) or not s.get("mc_version") or s["mc_version"].upper() == "LATEST":
        return []
    return list(TARGETS.get(current(s), ()))


async def check(s: dict, target: str) -> dict:
    """Is there a build of the target for this Minecraft version?
    {"ok", "note", "channel"}: channel "experimental" when Paper only has beta builds."""
    mc = s["mc_version"]
    out = {"ok": True, "note": "", "channel": None}
    try:
        async with httpx.AsyncClient(timeout=10, transport=_transport) as c:
            if target == "fabric":
                r = await c.get(f"https://meta.fabricmc.net/v2/versions/loader/{mc}")
                if r.status_code != 200 or not r.json():
                    return {**out, "ok": False, "note": f"Fabric doesn't support Minecraft {mc} yet."}
            elif target == "paper":
                r = await c.get(f"https://fill.papermc.io/v3/projects/paper/versions/{mc}/builds")
                builds = r.json() if r.status_code == 200 else []
                if isinstance(builds, dict):
                    builds = builds.get("builds") or []
                if not builds:
                    return {**out, "ok": False, "note": f"Paper has no builds for Minecraft {mc} yet."}
                chans = {str(b.get("channel", "")).upper() for b in builds}
                if not chans & {"STABLE", "RECOMMENDED"}:
                    best = "beta" if "BETA" in chans else "early test (alpha)"
                    out.update(channel="experimental",
                               note=f"Paper for {mc} only has {best} builds so far. Expect bugs, and keep the backup.")
            elif target == "purpur":
                r = await c.get(f"https://api.purpurmc.org/v2/purpur/{mc}")
                if r.status_code != 200 or not (r.json().get("builds") or {}).get("all"):
                    return {**out, "ok": False, "note": f"Purpur has no builds for Minecraft {mc} yet."}
    except Exception as e:                              # the check is a courtesy: never block on it
        out["note"] = f"Couldn't check whether {NAMES[target]} supports {mc} ({e.__class__.__name__}); the server's log will say if it doesn't."
    return out


def _clear_launcher(sid: str, old: str) -> None:
    """Remove the old software's launcher, so it isn't started and isn't detected again."""
    d = dockerctl.server_dir(sid)
    gone = []
    if old in ("fabric", "quilt"):
        gone += glob.glob(os.path.join(d, "fabric-server*.jar")) + glob.glob(os.path.join(d, "quilt-server*.jar"))
        gone += [os.path.join(d, ".fabric"), os.path.join(d, ".quilt"), os.path.join(d, "libraries", "net", "fabricmc"),
                 os.path.join(d, "libraries", "org", "quiltmc")]
    elif old in ("paper", "purpur"):
        gone += glob.glob(os.path.join(d, f"{old}*.jar"))
    for p in gone:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)


async def switch(sid: str, target: str) -> dict:
    s = state.get(sid)
    if target not in choices(s):
        raise ValueError(f"This server can't switch to {NAMES.get(target, target)} from here")
    if (s.get("software") or {}).get("step") in ("backup", "switch"):
        raise ValueError("A switch is already running for this server")
    c = await check(s, target)
    if not c["ok"]:
        raise ValueError(c["note"])
    state.update(sid, software={"step": "backup", "from": current(s), "target": target, "at": time.time(),
                                "msg": "Backing up first", "note": c["note"]})
    _tasks[sid] = asyncio.create_task(_run(sid, target, c["channel"]))
    return {"ok": True, "note": c["note"]}


async def _run(sid: str, target: str, channel: str | None) -> None:
    from . import activity
    s = state.get(sid)
    old = current(s)
    sw = dict(s.get("software") or {})
    try:
        snap = await backups.create(sid, f"before-{target}")
        sw.update(step="switch", msg=f"Switching to {NAMES[target]}")
        state.update(sid, software=sw)
        was_running = dockerctl.status(sid) == "running"
        if was_running:
            await servers.stop(sid)
        s = state.get(sid)
        prev = {k: s.get(k) for k in ("kind", "loader", "loader_version", "paper_channel", "managed_mods")}
        prev.update(backup=snap.get("id"), at=time.time(), name=NAMES.get(old, old))
        await asyncio.to_thread(_clear_launcher, sid, old)
        changes = {"kind": target, "loader": target, "loader_version": None, "software_prev": prev, "mod_updates": None,
                   "paper_channel": channel if target == "paper" else None}
        if target == "vanilla":
            changes["managed_mods"] = []           # vanilla has no mod loader to install them for
        sw.update(step="done", msg=f"Switched from {NAMES.get(old, old)} to {NAMES[target]}", restarted=was_running)
        state.update(sid, software=sw, **changes)
        activity.record("SpawnPoint", "system", "", "server.software", sid, f"{NAMES.get(old, old)} → {NAMES[target]}", True)
        if was_running:
            await servers.start(sid)
    except Exception as e:
        log.exception("[%s] switching to %s failed", sid, target)
        sw.update(step="failed", msg=f"The switch stopped: {str(e)[:200]}. The server wasn't changed.")
        state.update(sid, software=sw)


async def undo(sid: str) -> dict:
    """Put the world and the old type back from the backup made before the switch."""
    s = state.get(sid)
    prev = s.get("software_prev")
    if not prev or not prev.get("backup"):
        raise ValueError("There's no switch to undo")
    was_running = dockerctl.status(sid) == "running"
    if was_running:
        await servers.stop(sid)
    await asyncio.to_thread(_clear_launcher, sid, current(s))
    await backups.restore(sid, prev["backup"])
    state.update(sid, software_prev=None, software=None, mod_updates=None,
                 **{k: prev.get(k) for k in ("kind", "loader", "loader_version", "paper_channel", "managed_mods")})
    if was_running:
        await servers.start(sid)
    return {"restarted": was_running, "name": prev.get("name")}
