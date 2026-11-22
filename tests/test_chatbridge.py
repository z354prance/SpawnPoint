import asyncio
import json
import os

import httpx
import pytest

from app import chatbridge, dockerctl, servers, state


def test_parse_console_lines():
    p = chatbridge.parse
    assert p("[12:00:01] [Server thread/INFO]: <Steve> hello there") == ("chat", "Steve", "hello there")
    assert p("[12:00:01] [Server thread/INFO] [minecraft/MinecraftServer]: [Not Secure] <Alex> hi") == ("chat", "Alex", "hi")
    assert p("[12:00:01] [Server thread/INFO]: Steve joined the game") == ("joins", "Steve", "joined the game")
    assert p("[12:00:01] [Server thread/INFO]: .BedrockGuy left the game") == ("joins", ".BedrockGuy", "left the game")
    assert p("[12:00:01] [Server thread/INFO]: Steve has made the advancement [Stone Age]") == \
        ("advancements", "Steve", "made the advancement **Stone Age**")
    assert p("[12:00:01] [Server thread/INFO]: Steve was slain by Zombie") == ("deaths", "Steve", "was slain by Zombie")
    assert p("[12:00:01] [Server thread/INFO]: Steve fell from a high place") == ("deaths", "Steve", "fell from a high place")
    assert p("[12:00:01] [Server thread/INFO]: Saving the game") is None
    assert p("[12:00:01] [Server thread/WARN]: <Steve> fake") is None


def test_settings_hide_secrets_and_check_input(tmp_path, monkeypatch):
    monkeypatch.setattr(chatbridge.config, "STATE_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        chatbridge.set_server("a", webhook="https://example.com/hook")
    with pytest.raises(ValueError):
        chatbridge.set_server("a", channel="general")
    pub = chatbridge.set_server("a", on=True, webhook="https://discord.com/api/webhooks/1/abc-DEF", channel="123")
    assert pub["has_webhook"] and "webhook" not in pub and pub["channel"] == "123"
    chatbridge.set_bot("tok")
    assert chatbridge.public("a")["has_bot"] and "tok" not in json.dumps(chatbridge.public("a"))
    assert oct(os.stat(tmp_path / "chat.json").st_mode & 0o777) == "0o600"


def test_bridge_both_ways(tmp_path, monkeypatch):
    monkeypatch.setattr(chatbridge.config, "STATE_DIR", str(tmp_path))
    state.put({"id": "a", "name": "Survival", "phase": "online", "pregen": {"stage": "done"}})
    chatbridge.set_server("a", on=True, webhook="https://discord.com/api/webhooks/1/abc", channel="99")
    chatbridge.set_bot("tok")
    hub = dockerctl.LogHub("a")
    monkeypatch.setattr(dockerctl, "hub", lambda sid: hub)
    said = []

    async def arcmd(s, cmd):
        said.append(cmd)
        return ""
    monkeypatch.setattr(servers, "arcmd", arcmd)
    posted, discord = [], {"msgs": [{"id": "10", "content": "old", "author": {"username": "x"}}]}

    def handler(req):
        if req.url.path.startswith("/api/webhooks/"):
            posted.append(json.loads(req.content))
            return httpx.Response(204)
        if req.url.path == "/api/v10/channels/99/messages":
            assert req.headers["Authorization"] == "Bot tok"
            return httpx.Response(200, json=discord["msgs"])
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cl:
            await chatbridge.tick(cl, "tok", 0)                 # first look: remember where things are
            hub._push("[12:00:01] [Server thread/INFO]: <Steve> hello @everyone")
            hub._push("[12:00:02] [Server thread/INFO]: Steve was shot by Skeleton")
            discord["msgs"] = [{"id": "11", "content": "hi <@5>", "author": {"username": "kim", "global_name": "Kim"},
                                "mentions": [{"id": "5", "username": "steve_d", "global_name": "Steve"}]},
                               {"id": "12", "content": "echo", "webhook_id": "1", "author": {"username": "Steve", "bot": True}},
                               {"id": "13", "content": "beep", "author": {"username": "bot", "bot": True}}]
            await chatbridge.tick(cl, "tok", 1)
    asyncio.run(go())
    assert posted[0]["username"] == "Steve" and posted[0]["content"] == "hello @​everyone"
    assert posted[0]["allowed_mentions"] == {"parse": []}
    assert posted[1]["content"] == "**Steve** was shot by Skeleton"
    assert len(said) == 1 and said[0].startswith("tellraw @a ")
    msg = json.loads(said[0][len("tellraw @a "):])
    assert msg[1]["text"] == "Kim" and msg[2]["text"] == ": hi @Steve"
