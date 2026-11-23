"""Modpack / mod catalogs: Modrinth (no key), CurseForge (needs API key), FTB (by id)."""
import json
import os
import re

import httpx

from . import config

UA = {"User-Agent": "spawnpoint/0.1 (self-hosted Minecraft panel)"}
MR = "https://api.modrinth.com/v2"
CF = "https://api.curseforge.com/v1"
FTB = "https://api.feed-the-beast.com/v1/modpacks/public/modpack"

# Loader names as Modrinth uses them for each server kind
LOADER_FACET = {"paper": "paper", "purpur": "purpur", "fabric": "fabric", "quilt": "quilt",
                "forge": "forge", "neoforge": "neoforge"}


def _mr_hit(h: dict) -> dict:
    return {
        "source": "modrinth", "id": h["project_id"], "slug": h.get("slug"), "title": h.get("title"),
        "description": h.get("description"), "icon": h.get("icon_url"), "downloads": h.get("downloads"),
        "versions": h.get("versions", [])[-5:], "client_side": h.get("client_side"),
        "server_side": h.get("server_side"), "categories": h.get("categories", []),
    }


async def search_modpacks(query: str, source: str = "modrinth") -> list[dict]:
    async with httpx.AsyncClient(timeout=15, headers=UA) as c:
        if source == "curseforge":
            if not config.CF_API_KEY:
                return []
            r = await c.get(f"{CF}/mods/search", headers={"x-api-key": config.CF_API_KEY},
                            params={"gameId": 432, "classId": 4471, "searchFilter": query,
                                    "sortField": 2, "sortOrder": "desc", "pageSize": 30})
            r.raise_for_status()
            return [{
                "source": "curseforge", "id": m["id"], "slug": m["slug"], "title": m["name"],
                "description": m.get("summary"), "icon": (m.get("logo") or {}).get("thumbnailUrl"),
                "downloads": m.get("downloadCount"),
                "versions": sorted({v for f in m.get("latestFilesIndexes", []) for v in [f.get("gameVersion")] if v})[-5:],
            } for m in r.json().get("data", [])]
        facets = json.dumps([["project_type:modpack"]])
        r = await c.get(f"{MR}/search", params={"query": query, "facets": facets, "limit": 30,
                                                "index": "relevance" if query else "downloads"})
        r.raise_for_status()
        return [_mr_hit(h) for h in r.json().get("hits", [])]


async def modrinth_pack_version(project: str, version_id: str | None = None) -> dict:
    """Latest (or given) version of a Modrinth modpack: game version + loader."""
    async with httpx.AsyncClient(timeout=15, headers=UA) as c:
        if version_id:
            r = await c.get(f"{MR}/version/{version_id}")
            r.raise_for_status()
            v = r.json()
        else:
            r = await c.get(f"{MR}/project/{project}/version")
            r.raise_for_status()
            vs = r.json()
            v = next((x for x in vs if x.get("version_type") == "release"), vs[0] if vs else {})
    return {"id": v.get("id"), "name": v.get("version_number"),
            "mc_version": (v.get("game_versions") or [""])[0],
            "loader": (v.get("loaders") or [""])[0]}


async def curseforge_pack_info(mod_id: int) -> dict:
    async with httpx.AsyncClient(timeout=15, headers={**UA, "x-api-key": config.CF_API_KEY}) as c:
        r = await c.get(f"{CF}/mods/{mod_id}")
        r.raise_for_status()
        m = r.json()["data"]
    # The newest file is what gets installed; latestFiles isn't sorted by date.
    files = sorted(m.get("latestFiles", []), key=lambda f: f.get("fileDate", ""), reverse=True)
    gv = [v for f in files[:1] for v in f.get("gameVersions", [])]
    mc = next((v for v in gv if v[:1].isdigit()), "")
    loader = next((v.lower() for v in gv if v.lower() in ("forge", "neoforge", "fabric", "quilt")), "")
    top = files[0] if files else {}
    return {"slug": m["slug"], "title": m["name"], "mc_version": mc, "loader": loader,
            "file_id": top.get("id"), "version_name": _cf_version_name(top)}


def _cf_version_name(f: dict) -> str:
    """A readable version from a CurseForge file: its display name without the pack name noise."""
    name = (f.get("displayName") or f.get("fileName") or "").removesuffix(".zip")
    m = re.search(r"\d+(?:\.\d+)+[\w.-]*", name)
    return m.group(0) if m else name


async def ftb_pack_info(pack_id: int) -> dict:
    async with httpx.AsyncClient(timeout=15, headers=UA) as c:
        r = await c.get(f"{FTB}/{pack_id}")
        r.raise_for_status()
        p = r.json()
        versions = p.get("versions", [])
        latest = versions[-1] if versions else {}
        mc, loader = "", ""
        if latest:
            rv = await c.get(f"{FTB}/{pack_id}/{latest['id']}")
            if rv.status_code == 200:
                for t in rv.json().get("targets", []):
                    if t.get("type") == "game":
                        mc = t.get("version", "")
                    if t.get("type") == "modloader":
                        loader = t.get("name", "")
    return {"title": p.get("name", f"FTB {pack_id}"), "version_id": latest.get("id"),
            "version_name": latest.get("name", ""), "mc_version": mc, "loader": loader}


async def search_mods(query: str, loader: str, mc_version: str) -> list[dict]:
    lf = LOADER_FACET.get(loader, loader)
    ptype = "plugin" if loader in ("paper", "purpur") else "mod"
    facets = [[f"project_type:{ptype}"], [f"categories:{lf}"],
              ["server_side:required", "server_side:optional"]]
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    async with httpx.AsyncClient(timeout=15, headers=UA) as c:
        r = await c.get(f"{MR}/search", params={"query": query, "facets": json.dumps(facets), "limit": 30})
        r.raise_for_status()
        return [_mr_hit(h) for h in r.json().get("hits", [])]


async def find_version(project: str, loader: str, mc_version: str) -> dict | None:
    """Newest Modrinth version of a project for this loader + game version, or None."""
    loaders = [LOADER_FACET.get(loader, loader)]
    if loader in ("paper", "purpur"):
        loaders = ["paper", "purpur", "spigot", "bukkit"]
    params = {"loaders": json.dumps(loaders)}
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    async with httpx.AsyncClient(timeout=15, headers=UA) as c:
        r = await c.get(f"{MR}/project/{project}/version", params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        vs = r.json()
        if not vs:
            return None
        # Stable builds first: a mod's newest upload is sometimes a beta or release candidate.
        v = next((x for x in vs if x.get("version_type") == "release"), vs[0])
        f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
        pr = await c.get(f"{MR}/project/{project}")
        info = pr.json() if pr.status_code == 200 else {}
    return {"project_id": v["project_id"], "slug": info.get("slug") or project, "version_id": v["id"],
            "version": v.get("version_number"), "filename": f["filename"],
            "title": info.get("title") or project, "client_side": info.get("client_side")}


async def download_mod(project: str, loader: str, mc_version: str, dest_dir: str) -> dict:
    """Download the newest file of a Modrinth project matching loader + game version."""
    loaders = [LOADER_FACET.get(loader, loader)]
    if loader in ("paper", "purpur"):
        loaders = ["paper", "purpur", "spigot", "bukkit"]
    params = {"loaders": json.dumps(loaders)}
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    async with httpx.AsyncClient(timeout=60, headers=UA, follow_redirects=True) as c:
        r = await c.get(f"{MR}/project/{project}/version", params=params)
        r.raise_for_status()
        versions = r.json()
        if not versions:
            raise ValueError(f"No version of {project} for {loader} {mc_version}")
        v = versions[0]
        f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, f["filename"])
        async with c.stream("GET", f["url"]) as resp:
            resp.raise_for_status()
            with open(path, "wb") as out:
                async for chunk in resp.aiter_bytes():
                    out.write(chunk)
        pr = await c.get(f"{MR}/project/{project}")
        client_side = pr.json().get("client_side") if pr.status_code == 200 else None
    return {"filename": f["filename"], "version": v.get("version_number"), "client_side": client_side}


async def mc_versions() -> list[str]:
    async with httpx.AsyncClient(timeout=15, headers=UA) as c:
        r = await c.get("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        r.raise_for_status()
        return [v["id"] for v in r.json()["versions"] if v["type"] == "release"]
