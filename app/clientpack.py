"""The official client version of a server's modpack, for player packs.

A modpack's server files leave out everything that only matters on players' computers:
the title screen and loading screen theming, default resource packs, minimaps, visual and
performance mods. For servers installed from CurseForge, FTB or Modrinth, player packs are
built from the pack's client version instead (the exact version the server runs), plus
mods added through the Mods tab. Custom and imported servers keep using the server's files.

Each pack version is prepared once on the server: the client mods are downloaded from
their usual download servers and the pack's own files unpacked, into
STATE_DIR/client-packs/<source>-<id>-<version>/files. Players then get everything from
SpawnPoint in one stream, as before."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import time
import zipfile
from urllib.parse import quote

import httpx

from . import catalog, config, servers

log = logging.getLogger("spawnpoint.clientpack")

PARALLEL = 8
CF_RESOURCEPACK, CF_SHADER, CF_WORLD = 12, 6552, 17
_tasks: dict[str, asyncio.Task] = {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), follow_redirects=True,
                             headers={"User-Agent": "SpawnPoint (github.com/z354prance/SpawnPoint)"})


def key(s: dict) -> str | None:
    """Which official pack version this server runs, or None for custom/imported servers."""
    p = s.get("pack") or {}
    kind = s.get("kind")
    if kind == "modrinth" and p.get("project") and p.get("version_id"):
        return f"modrinth-{p['project']}-{p['version_id']}"
    if kind == "curseforge" and p.get("id") and p.get("file_id"):
        return f"curseforge-{p['id']}-{p['file_id']}"
    if kind == "ftb" and p.get("id") and p.get("version_id"):
        return f"ftb-{p['id']}-{p['version_id']}"
    return None


def root() -> str:
    return os.path.join(config.STATE_DIR, "client-packs")


def pack_dir(k: str) -> str:
    return os.path.join(root(), k)


def status(s: dict) -> dict:
    """{"state": "ready"|"building"|"error"|"none", ...}. "none": not an official pack."""
    k = key(s)
    if not k:
        return {"state": "none"}
    try:
        with open(os.path.join(pack_dir(k), "status.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {"state": "building", "done": 0, "total": 0} if k in _tasks and not _tasks[k].done() else {"state": "missing"}
    if st.get("state") == "building" and (k not in _tasks or _tasks[k].done()):
        st = {"state": "missing"}                             # SpawnPoint restarted mid-way: start again
    return st


def manifest(s: dict) -> dict[str, dict]:
    """{path: {sha256, size}} for a ready pack."""
    with open(os.path.join(pack_dir(key(s)), "manifest.json")) as f:
        return json.load(f)


def file_path(s: dict, rel: str) -> str:
    return os.path.join(pack_dir(key(s)), "files", rel)


def ensure(s: dict) -> dict:
    """Start preparing this server's official pack if it isn't ready or being prepared."""
    st = status(s)
    if st["state"] in ("missing", "error") and not (st["state"] == "error" and time.time() - st.get("at", 0) < 600):
        k = key(s)
        _tasks[k] = asyncio.get_event_loop().create_task(_build(s, k))
        st = {"state": "building", "done": 0, "total": 0}
    return st


def _write_status(d: str, **st) -> None:
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "status.json.part")
    with open(tmp, "w") as f:
        json.dump({**st, "at": time.time()}, f)
    os.replace(tmp, os.path.join(d, "status.json"))


def _safe(rel: str) -> str | None:
    """A plain path inside the pack ("./mods/a.jar" is fine), or None for anything that
    would point outside it."""
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if rel.startswith("/") or ".." in rel.split("/"):
        return None
    rel = os.path.normpath(rel).replace(os.sep, "/")
    return None if rel in ("", ".") else rel


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ what's in each pack
# Each returns (downloads, archive): downloads are {path, urls, size, sha1?, sha512?}, and
# archive is the pack file whose overrides folders hold the pack's own configs and theming.
async def _modrinth(c: httpx.AsyncClient, s: dict) -> tuple[list[dict], bytes | None, list[str]]:
    r = await c.get(f"{catalog.MR}/version/{s['pack']['version_id']}")
    r.raise_for_status()
    f = next((x for x in r.json()["files"] if x.get("primary")), r.json()["files"][0])
    data = (await c.get(f["url"])).raise_for_status().content
    idx = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("modrinth.index.json"))
    out = []
    for x in idx.get("files", []):
        if (x.get("env") or {}).get("client") == "unsupported":
            continue
        out.append({"path": x["path"], "urls": x.get("downloads", []), "size": x.get("fileSize"),
                    "sha1": x.get("hashes", {}).get("sha1"), "sha512": x.get("hashes", {}).get("sha512")})
    return out, data, ["overrides/", "client-overrides/"]


def _forgecdn(fid: int, name: str) -> list[str]:
    return [f"https://edge.forgecdn.net/files/{fid // 1000}/{fid % 1000}/{quote(name)}",
            f"https://mediafilez.forgecdn.net/files/{fid // 1000}/{fid % 1000}/{quote(name)}"]


async def _cf_files(c: httpx.AsyncClient, file_ids: list[int]) -> dict[int, dict]:
    h = {"x-api-key": config.CF_API_KEY}
    out = {}
    for i in range(0, len(file_ids), 500):
        r = await c.post(f"{catalog.CF}/mods/files", json={"fileIds": file_ids[i:i + 500]}, headers=h)
        r.raise_for_status()
        out.update({f["id"]: f for f in r.json()["data"]})
    return out


async def _curseforge(c: httpx.AsyncClient, s: dict) -> tuple[list[dict], bytes | None, list[str]]:
    if not config.CF_API_KEY:
        raise RuntimeError("needs a CurseForge API key (CF_API_KEY)")
    h = {"x-api-key": config.CF_API_KEY}
    p = s["pack"]
    r = await c.get(f"{catalog.CF}/mods/{p['id']}/files/{p['file_id']}", headers=h)
    r.raise_for_status()
    pf = r.json()["data"]
    url = pf.get("downloadUrl") or _forgecdn(pf["id"], pf["fileName"])[0]
    data = (await c.get(url)).raise_for_status().content
    z = zipfile.ZipFile(io.BytesIO(data))
    man = json.loads(z.read("manifest.json"))
    ids = [int(x["fileID"]) for x in man.get("files", []) if x.get("required", True)]
    files = await _cf_files(c, ids)
    mods = {}
    mod_ids = sorted({f["modId"] for f in files.values()})
    for i in range(0, len(mod_ids), 500):
        rm = await c.post(f"{catalog.CF}/mods", json={"modIds": mod_ids[i:i + 500]}, headers=h)
        rm.raise_for_status()
        mods.update({m["id"]: m for m in rm.json()["data"]})
    out = []
    for fid in ids:
        f = files.get(fid)
        if not f:
            continue
        cls = (mods.get(f["modId"]) or {}).get("classId")
        folder = {CF_RESOURCEPACK: "resourcepacks", CF_SHADER: "shaderpacks"}.get(cls, "mods")
        if cls == CF_WORLD:
            continue
        sha1 = next((x["value"] for x in f.get("hashes", []) if x.get("algo") == 1), None)
        urls = ([f["downloadUrl"]] if f.get("downloadUrl") else []) + _forgecdn(f["id"], f["fileName"])
        out.append({"path": f"{folder}/{f['fileName']}", "urls": urls, "size": f.get("fileLength"), "sha1": sha1})
    return out, data, [(man.get("overrides") or "overrides").rstrip("/") + "/"]


async def _ftb(c: httpx.AsyncClient, s: dict) -> tuple[list[dict], bytes | None, list[str]]:
    p = s["pack"]
    r = await c.get(f"{catalog.FTB}/{p['id']}/{p['version_id']}")
    r.raise_for_status()
    out = []
    for f in r.json().get("files", []):
        if f.get("serveronly"):
            continue
        urls = [f["url"]] if f.get("url") else []
        cf = f.get("curseforge") or {}
        if cf.get("file"):
            urls += _forgecdn(int(cf["file"]), f["name"])
        out.append({"path": (f.get("path") or "./") + f["name"], "urls": urls, "size": f.get("size"), "sha1": f.get("sha1")})
    return out, None, []


# ------------------------------------------------------------------ preparing it
async def _fetch(c: httpx.AsyncClient, item: dict, dst: str) -> None:
    last = None
    for url in item["urls"]:
        try:
            tmp = dst + ".part"
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            h1, h5 = hashlib.sha1(), hashlib.sha512()
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
                        h1.update(chunk)
                        h5.update(chunk)
            if item.get("sha1") and h1.hexdigest() != item["sha1"].lower():
                raise ValueError("checksum didn't match")
            if item.get("sha512") and h5.hexdigest() != item["sha512"].lower():
                raise ValueError("checksum didn't match")
            os.replace(tmp, dst)
            return
        except Exception as e:
            last = e
    raise RuntimeError(f"{os.path.basename(dst)}: {last or 'no download link'}")


async def _build(s: dict, k: str) -> None:
    d = pack_dir(k)
    files_dir = os.path.join(d, "files")
    shutil.rmtree(files_dir, ignore_errors=True)
    _write_status(d, state="building", done=0, total=0)
    try:
        async with _client() as c:
            src = k.split("-", 1)[0]
            downloads, archive, overrides = await {"modrinth": _modrinth, "curseforge": _curseforge, "ftb": _ftb}[src](c, s)
            # the pack's own files (configs, theming, resource packs)
            if archive:
                z = zipfile.ZipFile(io.BytesIO(archive))
                for info in z.infolist():
                    pre = next((o for o in overrides if info.filename.startswith(o)), None)
                    if not pre or info.is_dir():
                        continue
                    rel = _safe(info.filename[len(pre):])
                    if rel:
                        dst = os.path.join(files_dir, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        with z.open(info) as a, open(dst, "wb") as b:
                            shutil.copyfileobj(a, b)
            items = [(x, _safe(x["path"])) for x in downloads]
            items = [(x, rel) for x, rel in items if rel]
            total = len(items)
            done, missing = 0, []
            sem = asyncio.Semaphore(PARALLEL)

            async def one(x, rel):
                nonlocal done
                async with sem:
                    try:
                        await _fetch(c, x, os.path.join(files_dir, rel))
                    except Exception as e:
                        missing.append(f"{rel} ({e})")
                    done += 1
                    if done % 10 == 0 or done == total:
                        _write_status(d, state="building", done=done, total=total)
            await asyncio.gather(*(one(x, rel) for x, rel in items))
        man = {}
        for dirpath, _, filenames in os.walk(files_dir):
            for f in filenames:
                p = os.path.join(dirpath, f)
                rel = os.path.relpath(p, files_dir).replace(os.sep, "/")
                man[rel] = {"sha256": await asyncio.to_thread(_sha256, p), "size": os.path.getsize(p)}
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump(man, f)
        _write_status(d, state="ready", files=len(man), missing=missing[:50])
        log.info("[%s] official client pack %s ready: %d files%s", s["id"], k, len(man),
                 f", {len(missing)} couldn't be downloaded" if missing else "")
        # older versions of the same pack aren't needed any more
        prefix = k.rsplit("-", 1)[0] + "-"
        for other in os.listdir(root()):
            if other.startswith(prefix) and other != k:
                shutil.rmtree(os.path.join(root(), other), ignore_errors=True)
    except Exception as e:
        log.warning("[%s] couldn't prepare the official client pack %s: %s", s["id"], k, e)
        _write_status(d, state="error", error=str(e)[:300])


def extras(s: dict) -> dict[str, str]:
    """Mods added through the Mods tab that players need, from the server's mods folder
    (SpawnPoint's own server helpers left out)."""
    from .playerpack import SERVER_ONLY
    helpers = set(servers.HELPER_MODIDS) | {"chunky", "spark", "haproxycompat", "proxy-protocol-support",
                                             "ng-haproxy-support", "automodpack", "lithium", "ferrite-core",
                                             "ferritecore", "modernfix"}
    mdir = servers.mods_dir(s)
    out = {}
    for m in s.get("managed_mods", []):
        if m.get("slug") in helpers or SERVER_ONLY.search(m.get("filename", "")):
            continue
        p = os.path.join(mdir, m.get("filename", ""))
        if os.path.isfile(p):
            out[f"mods/{m['filename']}"] = p
    return out
