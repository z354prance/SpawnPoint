import asyncio
import hashlib
import json

import httpx
import pytest

from app import dockerctl, playermods, playerpack, state

FAPI = b"fabric-api-jar"


def _ver(pid, fname, deps=()):
    return [{"id": pid + "-v", "project_id": pid, "version_type": "release",
             "files": [{"primary": True, "filename": fname, "url": f"https://cdn.modrinth.com/{fname}"}],
             "dependencies": [{"project_id": d, "dependency_type": t} for d, t in deps]}]


def _net(req):
    u = req.url
    if u.path == "/v2/version_files":
        hashes = json.loads(req.content)["hashes"]
        sha = hashlib.sha1(FAPI).hexdigest()
        return httpx.Response(200, json={sha: {"project_id": "P7dR8mSH"}} if sha in hashes else {})
    if u.path == "/v2/search":
        facets = json.loads(u.params["facets"])
        assert ["client_side:required", "client_side:optional"] in facets and ["versions:26.3"] in facets
        return httpx.Response(200, json={"hits": [{"project_id": "voxy", "slug": "voxy", "title": "Voxy", "description": "LODs"}]})
    if u.path.endswith("/version"):
        pid = u.path.split("/")[3]
        return httpx.Response(200, json={
            "voxy": _ver("voxy", "voxy-0.2.jar", [("sodium", "required"), ("P7dR8mSH", "required"), ("iris", "optional")]),
            "sodium": _ver("sodium", "sodium-0.7.jar"),
            "lonely": [],
        }.get(pid, []))
    if u.path.startswith("/v2/project/"):
        pid = u.path.split("/")[3]
        return httpx.Response(200, json={"slug": pid, "title": pid.capitalize()})
    if u.host == "cdn.modrinth.com":
        return httpx.Response(200, content=b"jar:" + u.path.encode())
    return httpx.Response(404)


@pytest.fixture
def srv(tmp_path, monkeypatch):
    d = tmp_path / "t"
    (d / "mods").mkdir(parents=True)
    (d / "mods" / "fabric-api-0.161.jar").write_bytes(FAPI)
    (d / "mods" / "lss-0.22.jar").write_bytes(b"lss")
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(d))
    monkeypatch.setattr(playermods, "_transport", httpx.MockTransport(_net))
    state.put({"id": "t", "name": "t", "kind": "fabric", "loader": "fabric", "mc_version": "26.3"})
    return d


def test_voxy_comes_with_sodium_but_not_what_the_server_has(srv):
    r = asyncio.run(playermods.add("t", "voxy"))
    assert [a["file"] for a in r["added"]] == ["voxy-0.2.jar", "sodium-0.7.jar"] and r["missing"] == []
    assert r["added"][1]["for"] == "Voxy"
    assert (srv / "player-mods" / "voxy-0.2.jar").exists() and not (srv / "mods" / "voxy-0.2.jar").exists()
    assert not (srv / "player-mods" / "fabric-api-0.161.jar").exists()           # the server already has it

    fl, _ = playerpack.server_files(state.get("t"))
    assert {"mods/voxy-0.2.jar", "mods/sodium-0.7.jar", "mods/lss-0.22.jar", "mods/fabric-api-0.161.jar"} <= set(fl)
    titles = {m["file"]: m for m in playermods.listing(state.get("t"))}
    assert titles["voxy-0.2.jar"]["title"] == "Voxy" and titles["sodium-0.7.jar"]["for"] == "Voxy"

    playermods.remove("t", "sodium-0.7.jar")
    assert not (srv / "player-mods" / "sodium-0.7.jar").exists()
    assert [m["file"] for m in state.get("t")["player_mods"]] == ["voxy-0.2.jar"]
    with pytest.raises(ValueError):
        playermods.remove("t", "../mods/lss-0.22.jar")
    assert (srv / "mods" / "lss-0.22.jar").exists()


def test_adding_again_doesnt_download_twice(srv):
    asyncio.run(playermods.add("t", "voxy"))
    r = asyncio.run(playermods.add("t", "voxy"))
    assert r["added"] == []


def test_no_build_for_this_version(srv):
    with pytest.raises(ValueError, match="no build"):
        asyncio.run(playermods.add("t", "lonely"))


def test_search_asks_for_client_mods(srv):
    hits = asyncio.run(playermods.search(state.get("t"), "voxy"))
    assert hits[0]["title"] == "Voxy"


def test_a_mod_the_server_already_has(srv):
    state.update("t", managed_mods=[{"slug": "voxygen", "project_id": "voxygen", "filename": "voxygen.jar"}])
    r = asyncio.run(playermods.add("t", "voxygen"))
    assert r["added"] == [] and r["already"] == "Voxygen"


def test_listing_shows_what_players_get_from_the_server(srv, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config, main, users
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    users._fails.clear(); users.ensure_bootstrap()
    asyncio.run(playermods.add("t", "voxy"))
    c = TestClient(main.app)
    c.post("/api/login", json={"username": "admin", "password": "pw"})
    r = c.get("/api/servers/t/player-mods").json()
    assert {m["file"] for m in r["mods"]} == {"voxy-0.2.jar", "sodium-0.7.jar"}
    assert "lss-0.22.jar" in r["from_server"] and "voxy-0.2.jar" not in r["from_server"]
