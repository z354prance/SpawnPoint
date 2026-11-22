"""Safe Minecraft version upgrades for plain (non-modpack) servers.

  1. plan(): what would change. The Java image, and for each mod or plugin from Modrinth
     whether there's a build for the new version.
  2. start(): back up, then copy the server to a trial folder (while the real one keeps
     running), update the copy's mods, and start the copy on the new version in its own
     container with no ports open.
  3. If the copy starts cleanly, the real server is stopped, switched the same way and
     started again. If it doesn't, nothing about the real server changes, and the reason
     is shown in plain words.

The pre-upgrade backup stays, so rollback() can put the old version and world back.
Modpack servers change version through pack updates instead."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time

import httpx

from . import backups, catalog, config, crashinfo, dockerctl, modupdates, perf, servers, state, topology

log = logging.getLogger("spawnpoint.upgrade")
TRIAL_TIMEOUT_S = 20 * 60
_running: set[str] = set()
_tasks: dict[str, asyncio.Task] = {}


def supported(s: dict) -> bool:
    return not servers.is_managed(s) and bool(s.get("mc_version")) and s.get("mc_version", "").upper() != "LATEST"


def _newer(a: str, b: str) -> bool:
    return perf.parse_version(a) > perf.parse_version(b)


def _trial_dir(sid: str) -> str:
    return os.path.join(config.DATA_DIR, f".trial-{sid}")


def _set(sid: str, **fields) -> None:
    u = dict(state.get(sid).get("upgrade") or {})
    u.update(fields)
    state.update(sid, upgrade=u)


async def plan(sid: str, target: str) -> dict:
    s = state.get(sid)
    if not supported(s):
        raise ValueError("Modpack servers change Minecraft version through a pack update")
    cur = s["mc_version"]
    if target == cur:
        raise ValueError(f"The server is already on {cur}")
    if not _newer(target, cur):
        raise ValueError("Minecraft can't move a world to an older version")
    mods = await _mod_plan(s, target)
    loader_mods = s.get("loader") in ("fabric", "quilt", "forge", "neoforge")
    missing = [m for m in mods if m["status"] == "none"]
    size = await asyncio.to_thread(_du, dockerctl.server_dir(sid))
    free = shutil.disk_usage(config.DATA_DIR).free
    return {"current": cur, "target": target, "java": perf.java_tag(target), "java_now": s.get("java_tag"),
            "mods": mods, "missing": len(missing), "missing_off": loader_mods,
            "size": size, "free": free, "fits": free > size * 1.2 + 2 * 2**30}


def _du(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


async def _mod_plan(s: dict, target: str) -> list[dict]:
    """Every jar: "update" (a build for the target exists), "same" (the current file
    already supports it), "none" (Modrinth has no build for it) or "unknown" (not from Modrinth)."""
    d = servers.mods_dir(s)
    jars = [f for f in servers.mod_files(s) if f.endswith(".jar")] if s.get("loader") != "vanilla" else []
    if not jars:
        return []
    by_hash = {await asyncio.to_thread(modupdates._sha1, os.path.join(d, f)): f for f in jars}
    async with httpx.AsyncClient(timeout=30, headers=catalog.UA) as c:
        cur = await modupdates._post(c, "/version_files", {"hashes": list(by_hash), "algorithm": "sha1"})
        new = await modupdates._post(c, "/version_files/update", {"hashes": list(by_hash), "algorithm": "sha1",
                                                                 "loaders": modupdates._loaders(s), "game_versions": [target]})
        pids = sorted({v["project_id"] for v in cur.values()})
        titles = {}
        if pids:
            r = await c.get(f"{catalog.MR}/projects", params={"ids": json.dumps(pids)})
            titles = {p["id"]: p.get("title") for p in r.json()} if r.status_code == 200 else {}
    out = []
    for h, f in sorted(by_hash.items(), key=lambda kv: kv[1].lower()):
        c0, n = cur.get(h), new.get(h)
        if not c0:
            out.append({"file": f, "title": f, "status": "unknown"})
            continue
        title = titles.get(c0["project_id"]) or f
        if not n:
            out.append({"file": f, "title": title, "status": "none", "from": c0.get("version_number", "")})
        elif n["id"] == c0["id"]:
            out.append({"file": f, "title": title, "status": "same", "from": c0.get("version_number", "")})
        else:
            pf = next((x for x in n["files"] if x.get("primary")), n["files"][0])
            out.append({"file": f, "title": title, "status": "update", "from": c0.get("version_number", ""),
                        "to": n.get("version_number", ""), "new_file": pf["filename"], "url": pf["url"],
                        "sha1": pf["hashes"].get("sha1", "")})
    return out


async def _apply_mods(mods_dir: str, mods: list[dict], turn_off_missing: bool, keep: str | None = None) -> list[str]:
    """Swap in the new builds (and turn off the ones without one) inside a mods folder."""
    changed = []
    async with httpx.AsyncClient(timeout=120, headers=catalog.UA, follow_redirects=True) as c:
        for m in mods:
            old = os.path.join(mods_dir, m["file"])
            if m["status"] == "update":
                r = await c.get(m["url"])
                r.raise_for_status()
                if m["sha1"] and hashlib.sha1(r.content).hexdigest() != m["sha1"]:
                    raise RuntimeError(f"The download of {m['title']} didn't match Modrinth's checksum")
                with open(os.path.join(mods_dir, m["new_file"]), "wb") as f:
                    f.write(r.content)
                if os.path.exists(old) and m["new_file"] != m["file"]:
                    if keep:
                        os.makedirs(keep, exist_ok=True)
                        shutil.move(old, os.path.join(keep, m["file"]))
                    else:
                        os.remove(old)
                changed.append(f"{m['title']} {m.get('from', '')} → {m.get('to', '')}")
            elif m["status"] == "none" and turn_off_missing and os.path.exists(old):
                os.rename(old, old + ".disabled")
                changed.append(f"{m['title']} turned off (no build for the new version)")
    return changed


def _copy(src: str, dst: str) -> None:
    shutil.rmtree(dst, ignore_errors=True)
    # reflink copies are instant on Btrfs/XFS/ZFS; cp falls back to a normal copy elsewhere
    r = subprocess.run(["cp", "-a", "--reflink=auto", src, dst], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Couldn't copy the server for the trial: {r.stderr.strip()[:200]}")
    shutil.rmtree(os.path.join(dst, ".spawnpoint", "old-mods"), ignore_errors=True)


def _trial_boot(s: dict, trial: str, target: str) -> tuple[bool, list[str]]:
    """Start the copy on the new version in its own container; True once it's up."""
    t = {**s, "mc_version": target, "java_tag": perf.java_tag(target), "loader_version": None, "first_boot": False,
         "extra_env": {**(s.get("extra_env") or {})}}
    name = dockerctl.cname(s["id"]) + "-trial"
    cl = dockerctl.client()
    try:
        cl.containers.get(name).remove(force=True)
    except Exception:
        pass
    image = f"{config.IMAGE}:{t['java_tag']}"
    try:
        cl.images.get(image)
    except Exception:
        cl.images.pull(config.IMAGE, tag=t["java_tag"])
    env = dockerctl.build_env(t, max(2, len(topology.allocator().cpus_for(s["id"])) or 4))
    env.pop("MODRINTH_PROJECTS", None)                        # the copy's mods folder already has what it needs
    c = cl.containers.create(image, name=name, environment=env,
                             volumes={dockerctl.host_path(trial): {"bind": "/data", "mode": "rw"}},
                             network=config.DOCKER_NETWORK,
                             mem_limit=f"{perf.container_limit_gb(s['memory']['max_gb'], s.get('mod_count'))}g",
                             labels={"spawnpoint.trial": s["id"]}, restart_policy={"Name": "no"}, stop_signal="SIGTERM")
    ok, lines = False, []
    try:
        c.start()
        deadline = time.time() + TRIAL_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(5)
            c.reload()
            out = c.logs(tail=600).decode("utf-8", "replace")
            if "]: Done (" in out or ": Done (" in out:
                ok = True
                break
            if c.status != "running":
                break
        lines = c.logs(tail=800).decode("utf-8", "replace").splitlines()
        if ok:
            time.sleep(10)                                     # let it settle: some mods fail just after "Done"
            c.reload()
            lines = c.logs(tail=800).decode("utf-8", "replace").splitlines()
            ok = c.status == "running" and not any("Exception in server tick loop" in l for l in lines)
    finally:
        try:
            c.stop(timeout=90)
        except Exception:
            pass
        try:
            c.remove(force=True)
        except Exception:
            pass
    return ok, lines


async def start(sid: str, target: str, turn_off_missing: bool = True) -> None:
    """The whole upgrade, as a background task; progress goes in s["upgrade"]."""
    if sid in _running:
        raise ValueError("An upgrade is already running for this server")
    p = await plan(sid, target)
    if not p["fits"]:
        raise ValueError(f"Not enough free disk for a trial copy ({p['size'] / 2**30:.1f} GB needed)")
    _running.add(sid)
    state.update(sid, upgrade={"target": target, "from": p["current"], "step": "backup", "at": time.time(),
                               "msg": "Backing up first", "result": None})
    # its own task: servers.spawn would cancel (and be cancelled by) the server's start/stop tasks
    _tasks[sid] = asyncio.create_task(_run(sid, p, turn_off_missing))


async def _run(sid: str, p: dict, turn_off_missing: bool) -> None:
    from . import activity, alerts
    target, trial = p["target"], _trial_dir(sid)
    try:
        snap = await backups.create(sid, f"before-upgrade-{target}")
        s = state.get(sid)
        _set(sid, step="copy", msg="Copying the server for a trial run")
        running = dockerctl.status(sid) == "running"
        if running:
            try:
                await servers.arcmd(s, "save-off")
                await servers.arcmd(s, "save-all flush")
            except Exception:
                pass
        try:
            await asyncio.to_thread(_copy, dockerctl.server_dir(sid), trial)
        finally:
            if running:
                try:
                    await servers.arcmd(s, "save-on")
                except Exception:
                    pass
        _set(sid, step="mods", msg="Updating the copy's mods")
        if p["mods"]:
            await _apply_mods(os.path.join(trial, os.path.basename(servers.mods_dir(s))), p["mods"], turn_off_missing)
        _set(sid, step="trial", msg=f"Starting the copy on {target} (big modpacks take a few minutes)")
        ok, lines = await asyncio.to_thread(_trial_boot, s, trial, target)
        if not ok:
            ex = await asyncio.to_thread(_explain, sid, trial, lines)
            _set(sid, step="failed", msg=f"The copy didn't start on {target}, so nothing was changed",
                 result={"ok": False, "explain": ex, "tail": [l for l in lines if l.strip()][-25:]})
            activity.record("SpawnPoint", "system", "", "server.upgrade", sid, f"Trial on {target} failed: {ex.get('title') or 'see the log'}", False)
            alerts.send("crash_gave_up", s.get("name", sid), f"Upgrading {s.get('name', sid)} to {target} didn't work",
                        f"The trial copy didn't start, so the server wasn't changed. {ex.get('title', '')} {ex.get('detail', '')}".strip(), "warn")
            return
        # the copy works: switch the real server the same way
        _set(sid, step="switch", msg=f"The copy started on {target}. Switching the server over")
        was_running = dockerctl.status(sid) == "running"
        if was_running:
            await servers.stop(sid)
        keep = os.path.join(dockerctl.server_dir(sid), ".spawnpoint", "old-mods", f"upgrade-{time.strftime('%Y%m%d-%H%M%S')}")
        changed = await _apply_mods(servers.mods_dir(s), p["mods"], turn_off_missing, keep) if p["mods"] else []
        prev = {"mc_version": p["current"], "java_tag": s.get("java_tag"), "loader_version": s.get("loader_version"),
                "backup": snap.get("id"), "at": time.time(), "to": target}
        state.update(sid, mc_version=target, java_tag=perf.java_tag(target), loader_version=None, upgrade_prev=prev,
                     mod_updates=None, restart_pending=False)
        _set(sid, step="done", msg=f"Upgraded to {target}", result={"ok": True, "changed": changed, "restarted": was_running})
        activity.record("SpawnPoint", "system", "", "server.upgrade", sid, f"Upgraded {p['current']} → {target}", True)
        if was_running:
            await servers.start(sid)
    except Exception as e:
        log.exception("[%s] upgrade failed", sid)
        _set(sid, step="failed", msg=f"The upgrade stopped: {str(e)[:200]}. The server wasn't changed.", result={"ok": False})
    finally:
        _running.discard(sid)
        await asyncio.to_thread(shutil.rmtree, trial, True)


def _explain(sid: str, trial: str, lines: list[str]) -> dict:
    s = state.get(sid)
    try:
        return crashinfo.explain(sid, lines, since=time.time() - TRIAL_TIMEOUT_S - 120, base=trial,
                                 mods_dir=os.path.join(trial, os.path.basename(servers.mods_dir(s))))
    except Exception:
        return {}


async def rollback(sid: str) -> dict:
    """Put back the version, mods and world from just before the upgrade."""
    s = state.get(sid)
    prev = s.get("upgrade_prev")
    if not prev or not prev.get("backup"):
        raise ValueError("There's no upgrade to roll back")
    was_running = dockerctl.status(sid) == "running"
    if was_running:
        await servers.stop(sid)
    await backups.restore(sid, prev["backup"])
    state.update(sid, mc_version=prev["mc_version"], java_tag=prev["java_tag"], loader_version=prev.get("loader_version"),
                 upgrade_prev=None, upgrade=None, mod_updates=None)
    if was_running:
        await servers.start(sid)
    return {"restarted": was_running}


def recover() -> None:
    """After SpawnPoint restarts: an upgrade that was cut short is reported, and its trial
    copy and container are cleaned up."""
    for sid, s in list(state.servers().items()):
        u = s.get("upgrade") or {}
        if u and u.get("step") not in ("done", "failed"):
            msg = ("SpawnPoint restarted while switching the server over. Check that it starts; "
                   "if not, roll back.") if u.get("step") == "switch" else \
                "SpawnPoint restarted during the trial, so the server wasn't changed. Try again."
            _set(sid, step="failed", msg=msg, result={"ok": False})
        shutil.rmtree(_trial_dir(sid), ignore_errors=True)
    try:
        for c in dockerctl.client().containers.list(all=True, filters={"label": "spawnpoint.trial"}):
            c.remove(force=True)
    except Exception:
        pass
