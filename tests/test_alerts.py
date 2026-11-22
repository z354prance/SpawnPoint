import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from app import alerts, config, main, state, users


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def post(url, body):
        out.append((url, body))
    monkeypatch.setattr(alerts, "post", post)
    alerts._last.clear()
    return out


def run_send(*a, **k):
    async def go():
        alerts.send(*a, **k)
        await asyncio.sleep(0.05)
    asyncio.run(go())


def test_defaults_and_save(sent):
    s = alerts.settings()
    assert s["url"] == "" and s["events"]["crash"] and not s["events"]["server_online"]
    with pytest.raises(ValueError):
        alerts.save("ftp://nope", {})
    alerts.save("https://discord.com/api/webhooks/1/abc", {"crash": True})
    assert oct(os.stat(alerts._path()).st_mode & 0o777) == "0o600"        # the URL is a secret
    assert alerts.settings()["events"] == {k: k == "crash" for k in alerts.EVENTS}


def test_discord_format_and_repeat_limit(sent):
    alerts.save("https://discord.com/api/webhooks/1/abc", {"crash": True})
    run_send("crash", "aoc", "aoc crashed and is restarting", "Reason: froze", "warn")
    run_send("crash", "aoc", "again", "", "warn")                        # within 15 minutes: dropped
    run_send("backup_failed", "aoc", "off", "", "bad")                   # not turned on: dropped
    assert len(sent) == 1
    body = sent[0][1]
    assert body["embeds"][0]["title"] == "aoc crashed and is restarting" and body["embeds"][0]["footer"]["text"] == "aoc"


def test_generic_webhook_json(sent):
    alerts.save("https://ntfy.example.com/hook", {"disk_low": True})
    run_send("disk_low", "", "Disk almost full", "3 GB left", "bad")
    assert sent[0][1]["event"] == "disk_low" and sent[0][1]["message"] == "3 GB left"


def test_alerts_api_is_admin_only(monkeypatch, tmp_path, sent):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    users._fails.clear(); users.ensure_bootstrap()
    c = TestClient(main.app)
    assert c.get("/api/alerts").status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
    r = c.put("/api/alerts", json={"url": "https://discord.com/api/webhooks/9/x", "events": {"crash": True}})
    assert r.status_code == 200 and r.json()["events"]["crash"]
    assert c.post("/api/alerts/test", json={}).status_code == 200 and sent[-1][1]["embeds"][0]["title"] == "SpawnPoint alerts work"
