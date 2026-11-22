import asyncio
import hashlib
import os

import httpx
import pytest

from app import modupdates, servers, state, dockerctl


@pytest.fixture
def srv(tmp_path, monkeypatch):
    d = tmp_path / "f"
    (d / "mods").mkdir(parents=True)
    old = b"old create jar"
    (d / "mods" / "create-6.0.3.jar").write_bytes(old)
    (d / "mods" / "homemade.jar").write_bytes(b"not on modrinth")
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(d))
    state.put({"id": "f", "name": "f", "kind": "plain", "loader": "fabric", "mc_version": "1.21.1", "phase": "stopped",
               "pregen": {"stage": "done"}})
    new = b"new create jar"
    h_old, h_new = hashlib.sha1(old).hexdigest(), hashlib.sha1(new).hexdigest()

    def handler(req: httpx.Request):
        if req.url.path == "/v2/version_files":
            return httpx.Response(200, json={h_old: {"id": "v1", "project_id": "P", "version_number": "6.0.3",
                                                     "date_published": "2026-01-01"}})
        if req.url.path == "/v2/version_files/update":
            return httpx.Response(200, json={h_old: {"id": "v2", "project_id": "P", "version_number": "6.0.4",
                                                     "date_published": "2026-03-01", "files": [{
                                                         "primary": True, "filename": "create-6.0.4.jar",
                                                         "url": "https://cdn.modrinth.com/create-6.0.4.jar",
                                                         "hashes": {"sha1": h_new}}]}})
        if req.url.path == "/v2/projects":
            return httpx.Response(200, json=[{"id": "P", "title": "Create", "slug": "create"}])
        if req.url.host == "cdn.modrinth.com":
            return httpx.Response(200, content=new)
        return httpx.Response(404)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return d


def test_check_update_and_roll_back(srv):
    out = asyncio.run(modupdates.check("f"))
    assert out["unknown"] == 1 and out["total"] == 2
    assert [(i["title"], i["current"], i["latest"]) for i in out["items"]] == [("Create", "6.0.3", "6.0.4")]
    r = asyncio.run(modupdates.update("f"))
    assert r["updated"][0]["new"] == "create-6.0.4.jar" and not r["failed"]
    assert sorted(os.listdir(srv / "mods")) == ["create-6.0.4.jar", "homemade.jar"]
    s = state.get("f")
    assert s["restart_pending"] and s["mod_updates"]["items"] == []
    modupdates.rollback("f")
    assert sorted(os.listdir(srv / "mods")) == ["create-6.0.3.jar", "homemade.jar"]
    assert (srv / "mods" / "create-6.0.3.jar").read_bytes() == b"old create jar"


def test_modpack_servers_are_left_to_the_pack(srv):
    state.update("f", kind="modrinth")
    assert not modupdates.supported(state.get("f"))
    assert asyncio.run(modupdates.check("f")) == {}
