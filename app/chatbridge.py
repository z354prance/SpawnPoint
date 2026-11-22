"""Discord chat bridge.

Game -> Discord: chat, joins and leaves, deaths, advancements and the server going up or
down are read from the console and posted through a channel webhook, under the player's
name and face.

Discord -> game: with a bot token (shared by all servers) SpawnPoint reads new messages
in each server's channel every few seconds and shows them in game with /tellraw. The bot
needs the Message Content intent and permission to read the channel. Messages from bots
and webhooks are skipped, so the bridge never echoes itself.

Settings live in STATE_DIR/chat.json (mode 600): webhook URLs and the bot token are
secrets, so they never go into the server list the page loads."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx

from . import config, dockerctl, servers, state

log = logging.getLogger("spawnpoint.chat")
API = "https://discord.com/api/v10"
EVENTS = {"chat": "Chat", "joins": "Joins and leaves", "deaths": "Deaths", "advancements": "Advancements",
          "status": "Server up and down"}
POLL_S = 3

_LINE = re.compile(r"/INFO\](?: \[[^\]]*\])?: (.*)$")
_CHAT = re.compile(r"^(?:\[Not Secure\] )?<([A-Za-z0-9_.*]{1,20})> (.+)$")
_JOIN = re.compile(r"^([A-Za-z0-9_.*]{1,20}) (joined|left) the game$")
_ADV = re.compile(r"^([A-Za-z0-9_.*]{1,20}) has (made the advancement|completed the challenge|reached the goal) \[(.+)\]$")
_DEATH = re.compile(r"^([A-Za-z0-9_.*]{1,20}) (was (?:slain|shot|killed|blown up|fireballed|impaled|stung|pummeled|squashed|squished|"
                    r"pricked|poked|skewered|struck by lightning|frozen|obliterated|roasted)|drowned|fell|burned|blew up|hit the ground|"
                    r"went (?:up in flames|off with a bang)|tried to swim in lava|starved|suffocated|experienced kinetic energy|"
                    r"withered away|froze to death|walked into|discovered the floor was lava|died|didn't want to live)(.*)$")


# ------------------------------------------------------------------ settings
def _path() -> str:
    return os.path.join(config.STATE_DIR, "chat.json")


def _load() -> dict:
    try:
        with open(_path()) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("bot_token", "")
    d.setdefault("servers", {})
    return d


def _save(d: dict) -> None:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path())


def server_conf(sid: str) -> dict:
    c = _load()["servers"].get(sid) or {}
    return {"on": bool(c.get("on")), "webhook": c.get("webhook", ""), "channel": c.get("channel", ""),
            "to_game": c.get("to_game", True) is not False,
            "events": {k: bool((c.get("events") or {}).get(k, True)) for k in EVENTS}}


def public(sid: str) -> dict:
    """What the page may see: no secrets, just whether they're set."""
    c, d = server_conf(sid), _load()
    return {"on": c["on"], "has_webhook": bool(c["webhook"]), "channel": c["channel"], "to_game": c["to_game"],
            "events": c["events"], "has_bot": bool(d["bot_token"]), "event_names": EVENTS,
            "status": _status.get(sid, "")}


def set_server(sid: str, on: bool | None = None, webhook: str | None = None, channel: str | None = None,
               to_game: bool | None = None, events: dict | None = None) -> dict:
    d = _load()
    c = d["servers"].setdefault(sid, {})
    if webhook is not None:
        webhook = webhook.strip()
        if webhook and not re.match(r"^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$", webhook):
            raise ValueError("That doesn't look like a Discord webhook address (https://discord.com/api/webhooks/...)")
        c["webhook"] = webhook
    if channel is not None:
        channel = channel.strip()
        if channel and not channel.isdigit():
            raise ValueError("The channel ID is a long number: right-click the channel and choose Copy Channel ID")
        c["channel"] = channel
    if to_game is not None:
        c["to_game"] = bool(to_game)
    if events is not None:
        c["events"] = {k: bool(events.get(k, True)) for k in EVENTS}
    if on is not None:
        c["on"] = bool(on)
    _save(d)
    _after.pop(sid, None)                                   # start reading the channel from "now"
    return public(sid)


def set_bot(token: str) -> None:
    d = _load()
    d["bot_token"] = (token or "").strip()
    _save(d)
    _after.clear()


def forget(sid: str) -> None:
    d = _load()
    if d["servers"].pop(sid, None) is not None:
        _save(d)


# ------------------------------------------------------------------ game -> Discord
def parse(line: str) -> tuple[str, str, str] | None:
    """(event, player, text) for a console line worth posting, or None."""
    m = _LINE.search(line)
    if not m:
        return None
    msg = m.group(1).strip()
    if c := _CHAT.match(msg):
        return "chat", c.group(1), c.group(2)
    if j := _JOIN.match(msg):
        return "joins", j.group(1), f"{'joined' if j.group(2) == 'joined' else 'left'} the game"
    if a := _ADV.match(msg):
        verb = {"made the advancement": "made the advancement", "completed the challenge": "completed the challenge",
                "reached the goal": "reached the goal"}[a.group(2)]
        return "advancements", a.group(1), f"{verb} **{a.group(3)}**"
    if d := _DEATH.match(msg):
        return "deaths", d.group(1), msg[len(d.group(1)) + 1:]
    return None


def _face(name: str) -> str:
    return f"https://mc-heads.net/avatar/{name.lstrip('.*')}/64"


async def _post(c: httpx.AsyncClient, url: str, body: dict) -> None:
    body = {**body, "allowed_mentions": {"parse": []}}       # nobody can ping @everyone from the game
    for _ in range(3):
        r = await c.post(url, json=body)
        if r.status_code == 429:
            await asyncio.sleep(min(10.0, float(r.json().get("retry_after", 1))))
            continue
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(f"Discord answered {r.status_code}", request=r.request, response=r)
        return


def _to_discord(ev: str, player: str, text: str) -> dict:
    text = text.replace("@", "@​")[:1900]
    if ev == "chat":
        return {"username": player, "avatar_url": _face(player), "content": text}
    return {"username": "Server", "content": f"**{player}** {text}" if ev != "status" else text,
            **({"avatar_url": _face(player)} if ev != "status" else {})}


# ------------------------------------------------------------------ Discord -> game
def _clean(msg: dict) -> str:
    text = msg.get("content") or ""
    for u in msg.get("mentions") or []:
        text = text.replace(f"<@{u['id']}>", "@" + (u.get("global_name") or u["username"]))
        text = text.replace(f"<@!{u['id']}>", "@" + (u.get("global_name") or u["username"]))
    text = re.sub(r"<a?:(\w+):\d+>", r":\1:", text)           # custom emoji
    text = re.sub(r"<#\d+>", "#channel", text)
    if msg.get("attachments"):
        text = (text + " [attachment]").strip()
    return " ".join(text.split())[:256]


def tellraw(author: str, text: str) -> str:
    return "tellraw @a " + json.dumps([{"text": "[Discord] ", "color": "#7289DA"},
                                       {"text": author, "color": "white"}, {"text": ": " + text, "color": "gray"}])


async def _read_channel(cl: httpx.AsyncClient, token: str, sid: str, channel: str) -> list[dict]:
    h = {"Authorization": f"Bot {token}"}
    if sid not in _after:
        r = await cl.get(f"{API}/channels/{channel}/messages", params={"limit": 1}, headers=h)
        _check(r, sid)
        msgs = r.json()
        _after[sid] = msgs[0]["id"] if msgs else "0"
        return []
    r = await cl.get(f"{API}/channels/{channel}/messages", params={"after": _after[sid], "limit": 50}, headers=h)
    _check(r, sid)
    msgs = sorted(r.json(), key=lambda m: int(m["id"]))
    if msgs:
        _after[sid] = msgs[-1]["id"]
    return [m for m in msgs if not m.get("webhook_id") and not (m.get("author") or {}).get("bot")]


def _check(r: httpx.Response, sid: str) -> None:
    if r.status_code == 401:
        raise RuntimeError("Discord didn't accept the bot token")
    if r.status_code == 403:
        raise RuntimeError("The bot can't read that channel: give it View Channel and Read Message History")
    if r.status_code == 404:
        raise RuntimeError("Discord doesn't know that channel ID")
    r.raise_for_status()


# ------------------------------------------------------------------ the loop
_after: dict[str, str] = {}             # last Discord message seen per server
_seen: dict[str, tuple[int, int]] = {}  # (id of the console buffer, last line) per server
_status: dict[str, str] = {}            # last problem, shown on the settings card
_phase: dict[str, str] = {}


async def tick(cl: httpx.AsyncClient, bot: str, now: float) -> None:
    for sid, s in list(state.servers().items()):
        c = server_conf(sid)
        if not c["on"]:
            continue
        phase = s.get("phase", "")
        was = _phase.get(sid)
        _phase[sid] = phase
        if was is not None and was != phase and c["webhook"] and c["events"]["status"]:
            if phase == "online":
                await _safe(sid, _post(cl, c["webhook"], {"username": s.get("name", sid), "content": "🟢 The server is online"}))
            elif was == "online" and phase in ("stopped", "sleeping", "error"):
                why = {"sleeping": " (asleep: it wakes when someone joins)", "error": " (it ran into a problem)"}.get(phase, "")
                await _safe(sid, _post(cl, c["webhook"], {"username": s.get("name", sid), "content": f"🔴 The server stopped{why}"}))
        if phase != "online":
            continue
        # console -> Discord
        h = dockerctl.hub(sid)
        ident, last = _seen.get(sid, (0, None))
        if ident != id(h) or last is None:
            last = h.seq                                    # a new console buffer: start from its end
        lines = h.since(last)
        _seen[sid] = (id(h), lines[-1][0] if lines else last)
        if c["webhook"]:
            for _, line in lines:
                p = parse(line)
                if p and c["events"].get(p[0]):
                    await _safe(sid, _post(cl, c["webhook"], _to_discord(*p)))
        # Discord -> game
        if bot and c["channel"] and c["to_game"]:
            try:
                for m in await _read_channel(cl, bot, sid, c["channel"]):
                    text = _clean(m)
                    if text:
                        a = m.get("author") or {}
                        await servers.arcmd(s, tellraw(a.get("global_name") or a.get("username", "?"), text))
                _status.pop(sid, None)
            except Exception as e:
                _status[sid] = str(e)[:200]


async def _safe(sid: str, coro) -> None:
    try:
        await coro
        if _status.get(sid, "").startswith("Couldn't post"):
            _status.pop(sid, None)
    except Exception as e:
        _status[sid] = f"Couldn't post to Discord: {str(e)[:150]}"


async def loop() -> None:
    async with httpx.AsyncClient(timeout=15) as cl:
        while True:
            try:
                if any(v.get("on") for v in _load()["servers"].values()):
                    await tick(cl, _load()["bot_token"], time.time())
            except Exception as e:
                log.warning("chat bridge: %s", e)
            await asyncio.sleep(POLL_S)


async def test(sid: str) -> dict:
    """Post a hello and, with a bot, read the channel once. Returns what worked."""
    c, d = server_conf(sid), _load()
    out = {"posted": None, "read": None}
    async with httpx.AsyncClient(timeout=15) as cl:
        if c["webhook"]:
            try:
                await _post(cl, c["webhook"], {"username": "SpawnPoint", "content": f"Chat bridge for **{state.get(sid).get('name', sid)}** is connected."})
                out["posted"] = "ok"
            except Exception as e:
                out["posted"] = str(e)[:200]
        if d["bot_token"] and c["channel"]:
            try:
                r = await cl.get(f"{API}/channels/{c['channel']}/messages", params={"limit": 1},
                                 headers={"Authorization": f"Bot {d['bot_token']}"})
                _check(r, sid)
                msgs = r.json()
                if msgs and not msgs[0].get("content") and not msgs[0].get("webhook_id") and not msgs[0].get("attachments"):
                    out["read"] = "The bot can see messages but not their text: turn on Message Content Intent for it"
                else:
                    out["read"] = "ok"
            except Exception as e:
                out["read"] = str(e)[:200]
    return out
