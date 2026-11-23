"""Mods for players only: client mods the player pack hands out but the server never loads.

Some server mods have a client half that only runs in a player's game: LOD Server Support
needs Voxy (and Voxy needs Sodium) on every client. Those can't go in the server's mods
folder, so they live in <server>/player-mods/. The game only loads mods/, and the player
pack adds player-mods/ to players' mods folder.

Adding one also adds the mods it requires that neither the server nor this list has yet.
What's already there is found by asking Modrinth about the jars' sha1s."""
from __future__ import annotations

import hashlib
import json
import logging
import os

import httpx

from . import catalog, dockerctl, servers, state

log = logging.getLogger("spawnpoint.playermods")
FOLDER = "player-mods"
_transport = None                                     # tests swap in a fake network
MAX_DEPTH = 3


def folder(s: dict) -> str:
    return os.path.join(dockerctl.server_dir(s["id"]), FOLDER)


def files(s: dict) -> dict[str, str]:
    """{path in the player pack: file on disk}."""
    d = folder(s)
    if not os.path.isdir(d):
        return {}
    return {f"mods/{f}": os.path.join(d, f) for f in sorted(os.listdir(d))
            if f.endswith(".jar") and os.path.isfile(os.path.join(d, f))}


def listing(s: dict) -> list[dict]:
    known = {m["file"]: m for m in s.get("player_mods", [])}
    out = []
    for rel, p in files(s).items():
        f = rel[5:]
        m = known.get(f, {})
        out.append({"file": f, "title": m.get("title") or f, "size": os.path.getsize(p), "for": m.get("for", "")})
    return out


def _client(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=60, headers=catalog.UA, follow_redirects=True, transport=_transport, **kw)


async def search(s: dict, query: str) -> list[dict]:
    """Modrinth mods for this loader and version that run in players' games."""
    loader = s.get("loader") or s.get("kind")
    facets = [["project_type:mod"], [f"categories:{catalog.LOADER_FACET.get(loader, loader)}"],
              ["client_side:required", "client_side:optional"]]
    if s.get("mc_version"):
        facets.append([f"versions:{s['mc_version']}"])
    async with _client() as c:
        r = await c.get(f"{catalog.MR}/search", params={"query": query, "facets": json.dumps(facets), "limit": 30})
        r.raise_for_status()
        return [catalog._mr_hit(h) for h in r.json().get("hits", [])]


def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _present(c: httpx.AsyncClient, s: dict) -> set[str]:
    """Modrinth project ids of the jars the server and the player list already have."""
    jars = []
    for d in (servers.mods_dir(s), folder(s)):
        if os.path.isdir(d):
            jars += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jar")]
    ids = {m.get("project_id") for m in s.get("managed_mods", []) + s.get("player_mods", [])} - {None}
    if jars:
        try:
            r = await c.post(f"{catalog.MR}/version_files", json={"hashes": [_sha1(j) for j in jars], "algorithm": "sha1"})
            if r.status_code == 200:
                ids |= {v.get("project_id") for v in r.json().values()}
        except httpx.HTTPError as e:
            log.info("[%s] couldn't check which mods are present: %s", s["id"], e)
    return ids


async def _version(c: httpx.AsyncClient, s: dict, project: str, exact: str | None = None,
                   before: str | None = None) -> dict | None:
    """The build to use. A dependency names its exact version when the mod lists one;
    otherwise it's the newest build from before the mod that needs it was released, since a
    mod is built against what existed then (Voxy breaks on a newer Sodium than its own)."""
    if exact:
        r = await c.get(f"{catalog.MR}/version/{exact}")
        if r.status_code == 200:
            return r.json()
    loader = s.get("loader") or s.get("kind")
    params = {"loaders": json.dumps([catalog.LOADER_FACET.get(loader, loader)])}
    if s.get("mc_version"):
        params["game_versions"] = json.dumps([s["mc_version"]])
    r = await c.get(f"{catalog.MR}/project/{project}/version", params=params)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    vs = r.json()
    if not vs:
        return None
    if before:
        older = [v for v in vs if (v.get("date_published") or "") <= before]
        return max(older, key=lambda v: v.get("date_published") or "") if older else vs[0]
    return next((v for v in vs if v.get("version_type") == "release"), vs[0])


async def add(sid: str, project: str) -> dict:
    """Download the mod and what it requires into player-mods/. {"added": [{file, title}], "missing": [...]}"""
    s = state.get(sid)
    d = folder(s)
    os.makedirs(d, exist_ok=True)
    added, missing = [], []
    async with _client() as c:
        have = await _present(c, s)

        async def one(pid: str, depth: int, needed_by: str, exact: str | None = None, before: str | None = None) -> None:
            if pid in have or depth > MAX_DEPTH:
                return
            have.add(pid)
            v = await _version(c, s, pid, exact, before)
            pr = await c.get(f"{catalog.MR}/project/{pid}")
            info = pr.json() if pr.status_code == 200 else {}
            title = info.get("title") or pid
            if not v:
                if depth == 0:
                    raise ValueError(f"{title} has no build for {s.get('loader')} {s.get('mc_version')}")
                missing.append(f"{title} (needed by {needed_by})")
                return
            f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
            path = os.path.join(d, os.path.basename(f["filename"]))
            async with c.stream("GET", f["url"]) as resp:
                resp.raise_for_status()
                with open(path + ".part", "wb") as out:
                    async for chunk in resp.aiter_bytes():
                        out.write(chunk)
            os.replace(path + ".part", path)
            entry = {"file": os.path.basename(path), "title": title, "project_id": v["project_id"], "version": v.get("version_number"),
                     "slug": info.get("slug") or pid, "for": needed_by if depth else ""}
            added.append(entry)
            for dep in v.get("dependencies") or []:
                if dep.get("dependency_type") == "required" and (dep.get("project_id") or dep.get("version_id")):
                    pid2 = dep.get("project_id")
                    if not pid2:                                   # only the version is named
                        vr = await c.get(f"{catalog.MR}/version/{dep['version_id']}")
                        pid2 = vr.json().get("project_id") if vr.status_code == 200 else None
                    if pid2:
                        await one(pid2, depth + 1, title, dep.get("version_id"), v.get("date_published"))

        if project in have:
            pr = await c.get(f"{catalog.MR}/project/{project}")
            title = (pr.json() if pr.status_code == 200 else {}).get("title") or project
            return {"added": [], "missing": [], "already": title}
        await one(project, 0, "")
    cur = [m for m in state.get(sid).get("player_mods", []) if m["file"] not in {a["file"] for a in added}]
    state.update(sid, player_mods=cur + added)
    from . import files as fm
    fm.own_like(d, dockerctl.server_dir(sid))
    return {"added": [{"file": a["file"], "title": a["title"], "for": a["for"]} for a in added], "missing": missing, "already": ""}


def remove(sid: str, file: str) -> None:
    s = state.get(sid)
    name = os.path.basename(file)
    p = os.path.join(folder(s), name)
    if not name.endswith(".jar") or not os.path.isfile(p):
        raise ValueError("That mod isn't in the players' list")
    os.remove(p)
    state.update(sid, player_mods=[m for m in s.get("player_mods", []) if m["file"] != name])
