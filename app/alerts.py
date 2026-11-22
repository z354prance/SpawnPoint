"""Alerts to Discord (or any webhook) when something needs attention.

Settings live in STATE_DIR/alerts.json and are changed by admins in the dashboard.
A Discord webhook URL gets a Discord message; any other URL gets a small JSON body:
  {"event": "crash", "server": "aoc", "title": "...", "message": "...", "level": "warn", "ts": 1700000000}
"""
import asyncio
import json
import logging
import os
import time

import httpx

from . import config

log = logging.getLogger("spawnpoint.alerts")

EVENTS = {
    "crash": "A server crashed and was restarted",
    "crash_gave_up": "A server keeps crashing, or crashed with restart off",
    "backup_failed": "A backup failed",
    "schedule_failed": "A scheduled task failed",
    "disk_low": "The disk is running out of space",
    "pack_update": "A modpack update is available",
    "mod_updates": "Mod or plugin updates are available",
    "lag_report": "A server lagged and a report was recorded",
    "server_online": "A server came online",
    "server_offline": "A server stopped or went to sleep",
}
DEFAULT_ON = {"crash", "crash_gave_up", "backup_failed", "schedule_failed", "disk_low", "pack_update", "mod_updates", "lag_report"}
COLORS = {"info": 0x5B8DEF, "ok": 0x22C55E, "warn": 0xFF9D4A, "bad": 0xE5484D}
REPEAT_S = 15 * 60                     # the same alert for the same server at most this often
_last: dict[tuple[str, str], float] = {}


def _path() -> str:
    return os.path.join(config.STATE_DIR, "alerts.json")


def settings() -> dict:
    try:
        with open(_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    on = data.get("events")
    return {"url": data.get("url", ""), "events": {k: (k in DEFAULT_ON if on is None else bool(on.get(k))) for k in EVENTS}}


def save(url: str, events: dict) -> dict:
    url = (url or "").strip()
    if url and not url.startswith(("https://", "http://")):
        raise ValueError("The webhook address should start with https://")
    data = {"url": url, "events": {k: bool(events.get(k)) for k in EVENTS}}
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)                 # the URL is a secret: anyone with it can post to the channel
    os.replace(tmp, _path())
    return settings()


def is_discord(url: str) -> bool:
    return "discord.com/api/webhooks/" in url or "discordapp.com/api/webhooks/" in url


def _body(url: str, event: str, server: str, title: str, message: str, level: str) -> dict:
    if is_discord(url):
        return {"username": "SpawnPoint", "embeds": [{
            "title": title[:250], "description": message[:3900], "color": COLORS.get(level, COLORS["info"]),
            "footer": {"text": server or "SpawnPoint"}, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}]}
    return {"event": event, "server": server, "title": title, "message": message, "level": level, "ts": int(time.time())}


async def post(url: str, body: dict) -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json=body)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(f"{r.status_code}: {r.text[:200]}", request=r.request, response=r)


async def _send(event: str, server: str, title: str, message: str, level: str) -> None:
    cfg = settings()
    if not cfg["url"] or not cfg["events"].get(event):
        return
    key = (event, server)
    now = time.time()
    if now - _last.get(key, 0) < REPEAT_S:
        return
    _last[key] = now
    try:
        await post(cfg["url"], _body(cfg["url"], event, server, title, message, level))
    except Exception as e:
        log.warning("alert %s failed: %s", event, e)


def send(event: str, server: str, title: str, message: str = "", level: str = "warn") -> None:
    """Fire and forget: alerts never hold up or break what triggered them."""
    try:
        asyncio.get_running_loop().create_task(_send(event, server, title, message, level))
    except RuntimeError:
        pass


async def test(url: str | None = None) -> None:
    url = (url or settings()["url"]).strip()
    if not url:
        raise ValueError("Add a webhook address first")
    await post(url, _body(url, "test", "", "SpawnPoint alerts work",
                          "You'll get a message here when a server crashes, a backup fails or the disk gets full.", "ok"))
