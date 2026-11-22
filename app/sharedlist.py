"""An optional invite list shared by several servers.

The list lives in STATE_DIR/shared-players.json. A server opts in from its Players tab and
can exclude people. SpawnPoint then keeps that server's whitelist in step: it adds the
shared players (minus exclusions) and later removes only names it added itself, so each
server's own whitelist entries are never touched.

Running servers are changed over the console (the game looks up the player); stopped ones
by editing whitelist.json, with the UUID from Mojang (online mode) or worked out from the
name the way the game does (offline mode).
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import uuid as uuidlib

import httpx

from . import config, dockerctl, rcon, state

log = logging.getLogger("spawnpoint.shared")
NAME = re.compile(r"^\.?[A-Za-z0-9_]{1,16}$")     # Java names; Floodgate's Bedrock names start with "."
_lock = asyncio.Lock()


def _path() -> str:
    return os.path.join(config.STATE_DIR, "shared-players.json")


def players() -> list[dict]:
    try:
        with open(_path()) as f:
            return json.load(f).get("players", [])
    except (OSError, ValueError):
        return []


def _save(ps: list[dict]) -> None:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"players": sorted(ps, key=lambda p: p["name"].lower())}, f, indent=1)
    os.replace(tmp, _path())


def settings(s: dict) -> dict:
    sl = s.get("shared_list") or {}
    return {"enabled": bool(sl.get("enabled")), "exclude": sorted(sl.get("exclude") or []), "applied": sorted(sl.get("applied") or [])}


# ------------------------------------------------------------------ editing the list
def add(names: list[str], uuids: dict | None = None) -> list[dict]:
    ps = players()
    have = {p["name"].lower() for p in ps}
    for n in names:
        n = n.strip()
        if not NAME.match(n):
            raise ValueError(f"“{n}” isn't a Minecraft name")
        if n.lower() not in have:
            ps.append({"name": n, **({"uuid": uuids[n]} if uuids and uuids.get(n) else {})})
            have.add(n.lower())
    _save(ps)
    return players()


def remove(name: str) -> list[dict]:
    _save([p for p in players() if p["name"].lower() != name.lower()])
    return players()


def server_whitelist(sid: str) -> list[dict]:
    try:
        with open(os.path.join(dockerctl.server_dir(sid), "whitelist.json")) as f:
            return [e for e in json.load(f) if isinstance(e, dict) and e.get("name")]
    except (OSError, ValueError):
        return []


def import_from(sid: str) -> int:
    """Add a server's current whitelist to the shared list. Returns how many were new."""
    before = {p["name"].lower() for p in players()}
    wl = [e for e in server_whitelist(sid) if NAME.match(e["name"])]
    add([e["name"] for e in wl], {e["name"]: e.get("uuid") for e in wl})
    return len({p["name"].lower() for p in players()} - before)


# ------------------------------------------------------------------ keeping servers in step
def _online_mode(sid: str) -> bool:
    from . import servers
    return dict(servers.read_properties(sid)).get("online-mode", "true") != "false"


def offline_uuid(name: str) -> str:
    """The UUID an offline-mode server gives a name (Java's UUID.nameUUIDFromBytes)."""
    b = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    b[6] = (b[6] & 0x0F) | 0x30
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuidlib.UUID(bytes=bytes(b)))


async def _mojang_uuid(name: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://api.mojang.com/users/profiles/minecraft/{name}")
        if r.status_code == 200:
            h = r.json()["id"]
            return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    except Exception:
        pass
    return None


async def sync(sid: str) -> dict:
    """Bring one server's whitelist in line with the shared list and its exclusions."""
    from . import servers
    async with _lock:
        s = state.get(sid)
        if not s:
            return {}
        sl = settings(s)
        shared = {p["name"].lower(): p for p in players()}
        want = set(shared) - {x.lower() for x in sl["exclude"]} if sl["enabled"] else set()
        applied = {x.lower() for x in sl["applied"]}
        current = {e["name"].lower() for e in server_whitelist(sid)}
        to_add = [shared[n] for n in sorted(want - current)]
        to_remove = sorted((applied - want) & current)
        live = dockerctl.status(sid) == "running" and s.get("phase") == "online"
        done_add, done_remove = [], []
        if live:
            for p in to_add:
                try:
                    await servers.arcmd(s, f"whitelist add {p['name']}")
                    done_add.append(p["name"].lower())
                except rcon.RconError:
                    live = False
                    break
            for n in to_remove if live else []:
                try:
                    await servers.arcmd(s, f"whitelist remove {n}")
                    done_remove.append(n)
                except rcon.RconError:
                    break
        if not live and (to_add or to_remove):
            path = os.path.join(dockerctl.server_dir(sid), "whitelist.json")
            wl = [e for e in server_whitelist(sid) if e["name"].lower() not in set(to_remove)]
            online = _online_mode(sid)
            for p in to_add:
                u = p.get("uuid") or (await _mojang_uuid(p["name"]) if online else offline_uuid(p["name"]))
                if not u:
                    log.info("[%s] couldn't look up %s; they'll be added when the server is running", sid, p["name"])
                    continue
                wl.append({"uuid": u, "name": p["name"]})
                done_add.append(p["name"].lower())
            done_remove = list(to_remove)
            if os.path.isdir(os.path.dirname(path)):
                with open(path + ".tmp", "w") as f:
                    json.dump(wl, f, indent=2)
                os.replace(path + ".tmp", path)
        after = (current | set(done_add)) - set(done_remove)
        # names this list added that are still on the server (someone may have removed one by hand)
        new_applied = sorted(((applied | set(done_add)) - set(done_remove)) & after)
        state.update(sid, shared_list={**(s.get("shared_list") or {}), "applied": new_applied})
        return {"added": done_add, "removed": done_remove}


async def sync_all() -> None:
    for sid, s in list(state.servers().items()):
        sl = settings(s)
        if sl["enabled"] or sl["applied"]:
            try:
                await sync(sid)
            except Exception as e:
                log.warning("[%s] shared list: %s", sid, e)


def set_server(sid: str, enabled: bool | None = None, exclude: list[str] | None = None) -> dict:
    s = state.get(sid)
    sl = dict(s.get("shared_list") or {})
    if enabled is not None:
        sl["enabled"] = enabled
    if exclude is not None:
        sl["exclude"] = sorted({x.strip() for x in exclude if NAME.match(x.strip())}, key=str.lower)
    state.update(sid, shared_list=sl)
    return settings(state.get(sid))
