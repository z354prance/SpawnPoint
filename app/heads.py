"""Player faces for the map, fetched once from mc-heads.net and cached for a day.

Going through SpawnPoint means each face is downloaded once, and players' browsers
don't talk to a third-party site."""
import os
import re
import time

import httpx

from . import config

NAME = re.compile(r"^[A-Za-z0-9_]{1,16}$")      # Java names; Bedrock (Floodgate) names get the letter instead
TTL = 86400
MISS_TTL = 3600                                  # don't keep asking for names that have no skin
URL = "https://mc-heads.net/avatar/{name}/32"


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
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
            r = await c.get(URL.format(name=name))
        if r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n" and len(r.content) < 200_000:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, path)
            return path
    except (httpx.HTTPError, OSError):
        pass
    try:
        open(miss, "w").close()
    except OSError:
        pass
    return path if os.path.exists(path) else None      # an older face beats none
