import os

import pytest
from fastapi.testclient import TestClient

from app import activity, config, main, state, users


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "first-password")
    monkeypatch.setattr(state, "_path", os.path.join(str(tmp_path), "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(main.time, "sleep", lambda s: None)      # skip the failed-login delay
    users._fails.clear()
    users.ensure_bootstrap()
    return TestClient(main.app)          # no "with": lifespan (docker) is not started


def login(c, name, pw):
    return c.post("/api/login", json={"username": name, "password": pw})


def test_bootstrap_admin_uses_admin_password(client):
    assert login(client, "admin", "first-password").json()["role"] == "admin"
    me = client.get("/api/me").json()
    assert me["name"] == "admin" and me["must_change_password"] is True


def test_old_single_password_login_still_works(client):
    # the old UI only sent a password; username defaults to admin
    assert client.post("/api/login", json={"password": "first-password"}).status_code == 200


def test_failed_logins_are_logged_and_rate_limited(client):
    for _ in range(5):
        assert login(client, "admin", "nope").status_code == 401
    assert login(client, "admin", "first-password").status_code == 429   # blocked for 10 min
    log = activity.read()
    assert sum(e["action"] == "login.failed" for e in log) == 5
    assert log[0]["action"] == "login.blocked" and not log[0]["ok"]


def test_roles_are_enforced_and_denials_logged(client):
    login(client, "admin", "first-password")
    for name, role in (("mod", "moderator"), ("view", "viewer")):
        assert client.post("/api/users", json={"username": name, "password": "password1", "role": role}).status_code == 200
    v = TestClient(main.app)
    login(v, "view", "password1")
    assert v.get("/api/servers").status_code == 200                       # viewers can look
    assert v.patch("/api/servers/nope", json={}).status_code == 403        # but not change
    assert v.get("/api/servers/nope/properties").status_code == 403       # or read secrets
    assert v.get("/api/users").status_code == 403
    m = TestClient(main.app)
    login(m, "mod", "password1")
    assert m.patch("/api/servers/nope", json={}).status_code == 404        # allowed, server missing
    assert m.delete("/api/servers/nope").status_code == 403                # deleting is admin-only
    assert m.get("/api/activity").status_code == 403
    denied = [e for e in activity.read() if e["action"] == "server.settings" and not e["ok"]]
    assert any(e["user"] == "view" and "not allowed" in e["detail"] for e in denied)


def test_username_lockout_survives_changing_address(client, monkeypatch):
    ips = iter(f"203.0.113.{i}" for i in range(100))
    monkeypatch.setattr(main, "client_ip", lambda request: next(ips))
    for _ in range(10):
        assert login(client, "admin", "nope").status_code == 401
    assert login(client, "admin", "first-password").status_code == 429


def test_password_change_signs_out_other_sessions(client):
    login(client, "admin", "first-password")
    other = TestClient(main.app)
    login(other, "admin", "first-password")
    r = client.post("/api/me/password", json={"current": "first-password", "new": "second-password"})
    assert r.status_code == 200
    assert client.get("/api/me").status_code == 200          # this session got a fresh cookie
    assert other.get("/api/me").status_code == 401           # the other one is signed out
    assert client.get("/api/me").json()["must_change_password"] is False


def test_last_admin_cannot_be_removed_or_demoted(client):
    login(client, "admin", "first-password")
    assert client.patch("/api/users/admin", json={"role": "viewer"}).status_code == 400
    assert client.delete("/api/users/admin").status_code == 400
    client.post("/api/users", json={"username": "boss", "password": "password1", "role": "admin"})
    assert client.patch("/api/users/boss", json={"role": "moderator"}).status_code == 200


def test_activity_log_records_who_did_what(client):
    login(client, "admin", "first-password")
    client.post("/api/users", json={"username": "jess", "password": "password1", "role": "moderator"})
    log = client.get("/api/activity").json()
    assert log[0]["action"] == "user.create" and log[0]["target"] == "jess" and log[0]["user"] == "admin"
    assert any(e["action"] == "login" for e in log)
    assert client.get("/api/activity?action=login").json()[0]["action"] == "login"


def test_bad_usernames_and_short_passwords_rejected(client):
    login(client, "admin", "first-password")
    assert client.post("/api/users", json={"username": "a b", "password": "password1", "role": "viewer"}).status_code == 400
    assert client.post("/api/users", json={"username": "ok", "password": "short", "role": "viewer"}).status_code == 400
    assert client.post("/api/users", json={"username": "ok", "password": "password1", "role": "god"}).status_code == 400
