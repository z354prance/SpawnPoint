"""Player faces for the map, cut from the player's own skin (from Mojang) and cached for
a day. mc-heads.net is the fallback. Going through SpawnPoint means each face is
downloaded once, and players' browsers don't talk to a third-party site."""
import base64
import io
import json
import logging
import os
import re
import time

import httpx

from . import config

log = logging.getLogger("spawnpoint.heads")
_transport = None                                # tests swap in a fake network

NAME = re.compile(r"^[A-Za-z0-9_]{1,16}$")      # Java names; Bedrock (Floodgate) names get the letter instead
TTL = 86400
MISS_TTL = 600                                   # try again after 10 minutes when nothing worked
URL = "https://mc-heads.net/avatar/{name}/32"
PROFILE = "https://api.mojang.com/users/profiles/minecraft/{name}"
SESSION = "https://sessionserver.mojang.com/session/minecraft/profile/{uuid}"
PNG = b"\x89PNG\r\n\x1a\n"


def cut_face(skin: bytes, size: int = 32) -> bytes:
    """The face from a skin (8x8 at 8,8) with the hat layer (8x8 at 40,8) on top, scaled up."""
    from PIL import Image
    im = Image.open(io.BytesIO(skin)).convert("RGBA")
    k = im.width // 64 or 1                      # HD skins are 128 or more wide
    face = im.crop((8 * k, 8 * k, 16 * k, 16 * k))
    hat = im.crop((40 * k, 8 * k, 48 * k, 16 * k))
    if hat.getextrema()[3][1] > 0:               # an all-clear hat layer is just "no hat"
        face.alpha_composite(hat)
    out = io.BytesIO()
    face.resize((size, size), Image.NEAREST).save(out, "PNG")
    return out.getvalue()


async def _from_mojang(c: httpx.AsyncClient, name: str) -> bytes | None:
    r = await c.get(PROFILE.format(name=name))
    if r.status_code != 200:
        raise RuntimeError(f"Mojang has no player called {name} ({r.status_code})")
    uuid = r.json()["id"]
    r = await c.get(SESSION.format(uuid=uuid))
    r.raise_for_status()
    prop = next((p for p in r.json().get("properties", []) if p.get("name") == "textures"), None)
    skin = json.loads(base64.b64decode(prop["value"]))["textures"].get("SKIN") if prop else None
    if not skin:
        return None                              # the default skin: the fallback draws Steve/Alex
    r = await c.get(skin["url"].replace("http://", "https://", 1))
    r.raise_for_status()
    return cut_face(r.content)


async def _from_mc_heads(c: httpx.AsyncClient, name: str) -> bytes | None:
    r = await c.get(URL.format(name=name))
    if r.status_code == 200 and r.content[:8] == PNG and len(r.content) < 200_000:
        return r.content
    raise RuntimeError(f"mc-heads.net answered {r.status_code} {r.headers.get('content-type', '')}")


def _dir() -> str:
    d = os.path.join(config.STATE_DIR, "heads")
    os.makedirs(d, exist_ok=True)
    return d


async def face(name: str) -> str | None:
    """Path to a 32x32 PNG of the player's face, or None."""
    if not NAME.match(name):
        return None
    path = os.path.join(_dir(), name.lower() + ".png")
    miss = path + ".miss"
    now = time.time()
    if os.path.exists(path) and now - os.path.getmtime(path) < TTL:
        return path
    if os.path.exists(miss) and now - os.path.getmtime(miss) < MISS_TTL:
        return path if os.path.exists(path) else None
    why = []
    async with httpx.AsyncClient(timeout=8, follow_redirects=True, transport=_transport) as c:
        for source in (_from_mojang, _from_mc_heads):
            try:
                png = await source(c, name)
            except Exception as e:                       # network, odd answers, a broken skin
                why.append(f"{source.__name__[6:]}: {e}")
                continue
            if png:
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(png)
                os.replace(tmp, path)
                return path
    log.info("no face for %s: %s", name, "; ".join(why) or "default skin")
    try:
        open(miss, "w").close()
    except OSError:
        pass
    return path if os.path.exists(path) else None      # an older face beats none
