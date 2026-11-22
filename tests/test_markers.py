from fastapi.testclient import TestClient

from app import config, main, mapper, state, users


def test_markers_api_and_public_map(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(mapper, "spawn_point", lambda sid: (0, 0))
    monkeypatch.setattr(mapper, "public_index", lambda sid: {"dims": {}})
    state.put({"id": "a", "name": "A", "phase": "stopped", "pregen": {"stage": "done"},
               "public_map": {"enabled": True, "token": "tok12345abc"}})
    users._fails.clear(); users.ensure_bootstrap()
    c = TestClient(main.app)
    assert c.post("/api/servers/a/markers", json={"name": "Base", "x": 1, "z": 2}).status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
    m = c.post("/api/servers/a/markers", json={"name": "  Main   base ", "x": 120, "z": -40}).json()
    assert m["name"] == "Main base" and m["by"] == "admin"
    hidden = c.post("/api/servers/a/markers", json={"name": "Secret", "x": 5, "z": 5, "public": False, "color": "#22C55E"}).json()
    assert c.post("/api/servers/a/markers", json={"name": "x", "x": 0, "z": 0, "color": "red"}).status_code == 400
    assert c.post("/api/servers/a/markers", json={"name": " ", "x": 0, "z": 0}).status_code == 400
    assert [x["name"] for x in c.get("/api/servers/a/map").json()["markers"]] == ["Main base", "Secret"]
    pub = c.get("/m/tok12345abc/info").json()["markers"]
    assert pub == [{"name": "Main base", "dim": "overworld", "x": 120, "z": -40, "color": "#FF9D4A"}]
    r = c.put(f"/api/servers/a/markers/{m['id']}", json={"name": "Home", "x": 1, "z": 1, "color": "#5B8DEF"})
    assert r.json()["name"] == "Home"
    assert c.delete(f"/api/servers/a/markers/{hidden['id']}").status_code == 200
    assert [x["name"] for x in state.get("a")["markers"]] == ["Home"]
