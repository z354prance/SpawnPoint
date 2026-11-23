import asyncio
import hashlib
import io
import json
import os
import zipfile

import httpx

from app import catalog, clientpack, config, dockerctl, playerpack, servers, state

JAR_A, JAR_B, RP = b"jar-a" * 100, b"jar-b" * 100, b"resource-pack"
sha1 = lambda b: hashlib.sha1(b).hexdigest()


def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return buf.getvalue()


def _setup(tmp_path, monkeypatch, handler, s):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "CF_API_KEY", "key")
    base = tmp_path / "srv"
    (base / "mods").mkdir(parents=True)
    (base / "mods" / "jei-extra.jar").write_bytes(b"jei")
    (base / "mods" / "Chunky-NeoForge-1.4.23.jar").write_bytes(b"chunky")
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(base))
    monkeypatch.setattr(servers, "mods_dir", lambda s: str(base / "mods"))
    monkeypatch.setattr(clientpack, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True))
    state.put({"id": "p", "name": "P", "mc_version": "1.21.1", "loader": "neoforge", "hostname": "p.example.com",
               "managed_mods": [{"slug": "jei", "filename": "jei-extra.jar", "project_id": "j"},
                                {"slug": "chunky", "filename": "Chunky-NeoForge-1.4.23.jar", "project_id": "c"}], **s})
    return state.get("p")


def _build(s):
    async def run():
        clientpack.ensure(s)
        await clientpack._tasks[clientpack.key(s)]
    asyncio.run(run())


def test_modrinth_pack(tmp_path, monkeypatch):
    mrpack = _zip({"modrinth.index.json": json.dumps({"files": [
        {"path": "mods/a.jar", "downloads": ["https://cdn.modrinth.com/a.jar"], "hashes": {"sha1": sha1(JAR_A)}, "fileSize": 500},
        {"path": "mods/server-only.jar", "downloads": ["https://cdn.modrinth.com/s.jar"], "env": {"client": "unsupported"}},
        {"path": "../evil.jar", "downloads": ["https://cdn.modrinth.com/a.jar"]}]}),
        "overrides/config/fancymenu/menu.txt": "theme", "client-overrides/options.txt": "lang:en_us",
        "overrides/../../escape.txt": "x"})

    def handler(req):
        u = str(req.url)
        if u.endswith("/version/v1"):
            return httpx.Response(200, json={"files": [{"url": "https://cdn.modrinth.com/pack.mrpack", "primary": True}]})
        return httpx.Response(200, content={"https://cdn.modrinth.com/pack.mrpack": mrpack, "https://cdn.modrinth.com/a.jar": JAR_A}[u])
    s = _setup(tmp_path, monkeypatch, handler, {"kind": "modrinth", "pack": {"project": "pk", "version_id": "v1"}})
    try:
        playerpack.files(s)
        assert False, "should be preparing"
    except playerpack.Preparing:
        pass
    _build(s)
    assert clientpack.status(s)["state"] == "ready"
    fl, left = playerpack.files(s)
    assert set(fl) == {"mods/a.jar", "config/fancymenu/menu.txt", "options.txt", "mods/jei-extra.jar"}   # no helpers, no server-only
    assert open(fl["mods/a.jar"], "rb").read() == JAR_A and not left
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "state" / "evil.jar").exists()


def test_curseforge_pack_and_bad_checksum(tmp_path, monkeypatch):
    pack = _zip({"manifest.json": json.dumps({"overrides": "overrides", "files": [
        {"projectID": 1, "fileID": 1001, "required": True}, {"projectID": 2, "fileID": 2002, "required": True},
        {"projectID": 3, "fileID": 3003, "required": True}]}), "overrides/config/x.toml": "x=1"})

    def handler(req):
        u = str(req.url)
        if u.endswith("/mods/9/files/77"):
            return httpx.Response(200, json={"data": {"id": 77, "fileName": "pack.zip", "downloadUrl": "https://dl/pack.zip"}})
        if u.endswith("/mods/files"):
            return httpx.Response(200, json={"data": [
                {"id": 1001, "modId": 1, "fileName": "a.jar", "downloadUrl": "https://dl/a.jar", "hashes": [{"algo": 1, "value": sha1(JAR_A)}]},
                {"id": 2002, "modId": 2, "fileName": "look.zip", "downloadUrl": None, "hashes": []},       # downloads turned off: forgecdn
                {"id": 3003, "modId": 3, "fileName": "b.jar", "downloadUrl": "https://dl/b.jar", "hashes": [{"algo": 1, "value": "0" * 40}]}]})
        if u.endswith("/mods"):
            return httpx.Response(200, json={"data": [{"id": 1, "classId": 6}, {"id": 2, "classId": clientpack.CF_RESOURCEPACK}, {"id": 3, "classId": 6}]})
        if "forgecdn.net/files/2/2/look.zip" in u:
            return httpx.Response(200, content=RP)
        return httpx.Response(200, content={"https://dl/pack.zip": pack, "https://dl/a.jar": JAR_A, "https://dl/b.jar": JAR_B}.get(u, b""))
    s = _setup(tmp_path, monkeypatch, handler, {"kind": "curseforge", "pack": {"id": 9, "file_id": 77}})
    _build(s)
    fl, left = playerpack.files(s)
    assert {"mods/a.jar", "resourcepacks/look.zip", "config/x.toml", "mods/jei-extra.jar"} <= set(fl)
    assert "mods/b.jar" not in fl and any("b.jar" in x for x in left)            # wrong checksum: left out, said why


def test_ftb_pack_and_custom_servers(tmp_path, monkeypatch):
    def handler(req):
        u = str(req.url)
        if u.endswith(f"/5/66"):
            return httpx.Response(200, json={"files": [
                {"path": "./mods/", "name": "a.jar", "url": "https://dl/a.jar", "sha1": sha1(JAR_A)},
                {"path": "./mods/", "name": "srv.jar", "url": "https://dl/s.jar", "serveronly": True},
                {"path": "./config/", "name": "c.toml", "url": "", "curseforge": {"project": 1, "file": 4567}}]})
        if "forgecdn.net/files/4/567/c.toml" in u:
            return httpx.Response(200, content=b"c=1")
        return httpx.Response(200, content=JAR_A)
    s = _setup(tmp_path, monkeypatch, handler, {"kind": "ftb", "pack": {"id": 5, "version_id": 66}})
    _build(s)
    assert set(playerpack.files(s)[0]) == {"mods/a.jar", "config/c.toml", "mods/jei-extra.jar"}
    # a new version replaces the old one's files
    state.update("p", pack={"id": 5, "version_id": 67})
    assert clientpack.key(state.get("p")) == "ftb-5-67"
    # custom and imported servers keep using their own files
    state.update("p", kind="imported", pack=None)
    assert clientpack.key(state.get("p")) is None
    assert "mods/jei-extra.jar" in playerpack.files(state.get("p"))[0]
