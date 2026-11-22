"""Updates for the mods and plugins on plain (non-modpack) servers.

Modrinth can match a jar by its SHA-1 and say what the newest compatible version is,
so this works for any jar that came from Modrinth, however it got there. Jars from
elsewhere are counted but left alone.

Updating keeps the old jars in .spawnpoint/old-mods/<time>/ so the whole batch can be
rolled back. Modpack servers are skipped: their pack decides the mod versions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time

import httpx

from . import catalog, config, dockerctl, servers, state

log = logging.getLogger("spawnpoint.mods")
CHECK_EVERY_S = 24 * 3600
_sha_cache: dict[tuple, str] = {}


def supported(s: dict) -> bool:
    return not servers.is_managed(s) and s.get("loader") not in ("vanilla", "", None)


def _sha1(path: str) -> str:
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if key not in _sha_cache:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _sha_cache[key] = h.hexdigest()
    return _sha_cache[key]


def _loaders(s: dict) -> list[str]:
    loader = s.get("loader") or ""
    if loader in ("paper", "purpur"):
        return ["paper", "purpur", "spigot", "bukkit"]
    return [catalog.LOADER_FACET.get(loader, loader)]


async def _post(c: httpx.AsyncClient, path: str, body: dict):
    r = await c.post(f"{catalog.MR}{path}", json=body)
    r.raise_for_status()
    return r.json()


async def check(sid: str) -> dict:
    """Look up every jar on Modrinth. Saves and returns {checked, items, unknown, total}."""
    s = state.get(sid)
    if not supported(s):
        return {}
    d = servers.mods_dir(s)
    jars = [f for f in servers.mod_files(s) if f.endswith(".jar")]
    by_hash = {}
    for f in jars:
        try:
            by_hash[await asyncio.to_thread(_sha1, os.path.join(d, f))] = f
        except OSError:
            pass
    items = []
    known = 0
    if by_hash:
        async with httpx.AsyncClient(timeout=30, headers=catalog.UA) as c:
            hashes = list(by_hash)
            current = await _post(c, "/version_files", {"hashes": hashes, "algorithm": "sha1"})
            latest = await _post(c, "/version_files/update", {
                "hashes": hashes, "algorithm": "sha1", "loaders": _loaders(s),
                "game_versions": [s["mc_version"]] if s.get("mc_version") else []})
            known = len(current)
            pids = sorted({v["project_id"] for v in latest.values()})
            titles = {}
            if pids:
                r = await c.get(f"{catalog.MR}/projects", params={"ids": json.dumps(pids)})
                if r.status_code == 200:
                    titles = {p["id"]: p for p in r.json()}
        for h, new in latest.items():
            old = current.get(h)
            if not old or new["id"] == old["id"]:
                continue
            if new.get("date_published", "") <= old.get("date_published", ""):
                continue                                   # never offer an older build as an "update"
            f = next((x for x in new["files"] if x.get("primary")), new["files"][0])
            p = titles.get(new["project_id"], {})
            items.append({"file": by_hash[h], "project_id": new["project_id"], "title": p.get("title") or by_hash[h],
                          "slug": p.get("slug", ""), "current": old.get("version_number", ""),
                          "latest": new.get("version_number", ""), "latest_file": f["filename"], "url": f["url"],
                          "sha1": f["hashes"].get("sha1", ""), "version_id": new["id"]})
    out = {"checked": time.time(), "items": sorted(items, key=lambda i: i["title"].lower()),
           "unknown": len(jars) - known, "total": len(jars)}
    state.update(sid, mod_updates=out)
    return out


async def update(sid: str, files: list[str] | None = None) -> dict:
    """Download the newer jars, swap them in and keep the old ones for a rollback."""
    s = state.get(sid)
    info = s.get("mod_updates") or await check(sid)
    todo = [i for i in info.get("items", []) if files is None or i["file"] in files]
    if not todo:
        return {"updated": [], "restart_needed": False}
    d = servers.mods_dir(s)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    keep = os.path.join(dockerctl.server_dir(sid), ".spawnpoint", "old-mods", stamp)
    os.makedirs(keep, exist_ok=True)
    swaps, failed = [], []
    async with httpx.AsyncClient(timeout=120, headers=catalog.UA, follow_redirects=True) as c:
        for i in todo:
            tmp = os.path.join(d, i["latest_file"] + ".part")
            try:
                h = hashlib.sha1()
                async with c.stream("GET", i["url"]) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 20):
                            f.write(chunk)
                            h.update(chunk)
                if i["sha1"] and h.hexdigest() != i["sha1"]:
                    raise ValueError("the download didn't match Modrinth's checksum")
                old = os.path.join(d, i["file"])
                if os.path.exists(old):
                    shutil.move(old, os.path.join(keep, i["file"]))
                os.replace(tmp, os.path.join(d, i["latest_file"]))
                swaps.append({"old": i["file"], "new": i["latest_file"], "title": i["title"],
                              "from": i["current"], "to": i["latest"]})
            except Exception as e:
                log.warning("[%s] updating %s failed: %s", sid, i["file"], e)
                failed.append({"file": i["file"], "title": i["title"], "error": str(e)[:200]})
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    if not swaps:
        shutil.rmtree(keep, ignore_errors=True)
    done = {x["old"] for x in swaps}
    left = [i for i in info.get("items", []) if i["file"] not in done]
    changes = {"mod_updates": {**info, "items": left}}
    if swaps:
        changes["mod_rollback"] = {"at": time.time(), "dir": stamp, "swaps": swaps}
        changes["restart_pending"] = True
    state.update(sid, **changes)
    return {"updated": swaps, "failed": failed, "restart_needed": bool(swaps)}


def rollback(sid: str) -> dict:
    """Put back the jars the last update replaced."""
    s = state.get(sid)
    rb = s.get("mod_rollback")
    if not rb:
        raise ValueError("There's no mod update to roll back")
    d = servers.mods_dir(s)
    keep = os.path.join(dockerctl.server_dir(sid), ".spawnpoint", "old-mods", rb["dir"])
    restored = []
    for sw in rb["swaps"]:
        src = os.path.join(keep, sw["old"])
        if not os.path.exists(src):
            continue
        try:
            os.remove(os.path.join(d, sw["new"]))
        except OSError:
            pass
        shutil.move(src, os.path.join(d, sw["old"]))
        restored.append(sw["old"])
    shutil.rmtree(keep, ignore_errors=True)
    state.update(sid, mod_rollback=None, mod_updates=None, restart_pending=True)
    return {"restored": restored, "restart_needed": True}


async def loop() -> None:
    """Check once a day, and say so when a server has updates waiting."""
    from . import alerts
    await asyncio.sleep(300)
    while True:
        for sid, s in list(state.servers().items()):
            if not supported(s) or s.get("first_boot") or s.get("phase") == "installing":
                continue
            if time.time() - ((s.get("mod_updates") or {}).get("checked") or 0) < CHECK_EVERY_S:
                continue
            try:
                before = {i["file"] for i in (s.get("mod_updates") or {}).get("items", [])}
                out = await check(sid)
                new = [i for i in out.get("items", []) if i["file"] not in before]
                if new:
                    names = ", ".join(f"{i['title']} {i['latest']}" for i in new[:8])
                    alerts.send("mod_updates", s.get("name", sid), f"Mod updates for {s.get('name', sid)}",
                                f"{len(out['items'])} mod(s) can be updated: {names}" + ("…" if len(new) > 8 else ""), "info")
            except Exception as e:
                log.info("[%s] mod update check failed: %s", sid, e)
            await asyncio.sleep(3)
        await asyncio.sleep(1800)
