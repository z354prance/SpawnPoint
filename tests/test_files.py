import io
import os
import tarfile
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import config, dockerctl, files, main, state, users


@pytest.fixture
def c(tmp_path, monkeypatch):
    base = tmp_path / "srv"
    (base / "world" / "region").mkdir(parents=True)
    (base / "world" / "region" / "r.0.0.mca").write_bytes(b"\x00" * 5000)
    (base / "config").mkdir()
    (base / "config" / "a.toml").write_text("x = 1\n")
    (base / "server.properties").write_text("motd=hi\n")
    (tmp_path / "outside.txt").write_text("secret")
    os.symlink(tmp_path / "outside.txt", base / "escape.txt")
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(base))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    state.put({"id": "a", "name": "A", "phase": "stopped", "pregen": {"stage": "done"}})
    users._fails.clear(); users.ensure_bootstrap()
    cl = TestClient(main.app)
    cl.post("/api/login", json={"username": "admin", "password": "pw"})
    cl.base = base
    return cl


def test_list_read_write(c):
    r = c.get("/api/servers/a/files").json()
    assert [e["name"] for e in r["entries"]][:2] == ["config", "world"] and r["entries"][0]["dir"]
    t = c.get("/api/servers/a/files/text", params={"path": "config/a.toml"}).json()
    assert t["content"] == "x = 1\n"
    assert c.put("/api/servers/a/files", json={"path": "config/a.toml", "content": "x = 2\n", "mtime": t["mtime"]}).status_code == 200
    # someone else changed it meanwhile
    assert c.put("/api/servers/a/files", json={"path": "config/a.toml", "content": "x = 3\n", "mtime": t["mtime"]}).status_code == 409
    assert c.put("/api/servers/a/files", json={"path": "config/new.toml", "content": "y\n", "create": True}).status_code == 200
    assert c.put("/api/servers/a/files", json={"path": "config/new.toml", "content": "y\n", "create": True}).status_code == 409
    assert c.get("/api/servers/a/files/text", params={"path": "world/region/r.0.0.mca"}).status_code == 415


def test_stays_inside(c):
    for p in ("../outside.txt", "escape.txt", "world/../../outside.txt"):
        assert c.get("/api/servers/a/files/text", params={"path": p}).status_code == 400, p
    assert c.post("/api/servers/a/files/delete", json={"paths": [""]}).status_code == 400
    assert c.post("/api/servers/a/files/rename", json={"path": "config", "name": "../x"}).status_code == 400
    assert c.put("/api/servers/a/files/upload", params={"path": "../evil.txt"}, content=b"x").status_code == 400


def test_mkdir_rename_move_copy(c):
    assert c.post("/api/servers/a/files/mkdir", json={"path": "stuff"}).status_code == 200
    assert c.post("/api/servers/a/files/mkdir", json={"path": "stuff"}).status_code == 409
    assert c.post("/api/servers/a/files/rename", json={"path": "config/a.toml", "name": "b.toml"}).json()["path"] == "config/b.toml"
    r = c.post("/api/servers/a/files/move", json={"paths": ["config/b.toml"], "to": "stuff", "mode": "copy"}).json()
    assert r["done"] == ["stuff/b.toml"] and (c.base / "config" / "b.toml").exists()
    r = c.post("/api/servers/a/files/move", json={"paths": ["stuff/b.toml"], "to": "stuff", "mode": "copy"}).json()
    assert r["done"] == ["stuff/b (copy).toml"]
    assert c.post("/api/servers/a/files/move", json={"paths": ["config/b.toml"], "to": "stuff"}).status_code == 409
    assert c.post("/api/servers/a/files/move", json={"paths": ["stuff"], "to": "stuff"}).status_code == 400
    c.post("/api/servers/a/files/move", json={"paths": ["config"], "to": "stuff"})
    assert (c.base / "stuff" / "config" / "b.toml").exists()


def test_delete_goes_to_recently_deleted(c):
    assert c.post("/api/servers/a/files/delete", json={"paths": ["world", "server.properties"]}).status_code == 200
    assert not (c.base / "world").exists()
    items = c.get("/api/servers/a/files/trash").json()["items"]
    assert {i["path"] for i in items} == {"world", "server.properties"} and any(i["size"] == 5000 for i in items)
    (c.base / "server.properties").write_text("new\n")         # something took its place meanwhile
    c.post("/api/servers/a/files/trash/restore", json={"ids": [i["id"] for i in items]})
    assert (c.base / "world" / "region" / "r.0.0.mca").exists()
    assert (c.base / "server (restored).properties").read_text() == "motd=hi\n"
    assert c.get("/api/servers/a/files/trash").json()["items"] == []


def test_upload_download_zip(c):
    r = c.put("/api/servers/a/files/upload", params={"path": "mods/new/thing.jar"}, content=b"J" * 100000)
    assert r.json() == {"path": "mods/new/thing.jar", "size": 100000}
    assert c.put("/api/servers/a/files/upload", params={"path": "mods/new/thing.jar"}, content=b"x").status_code == 409
    c.put("/api/servers/a/files/upload", params={"path": "mods/new/thing.jar", "overwrite": True}, content=b"x")
    assert (c.base / "mods" / "new" / "thing.jar").read_bytes() == b"x"
    assert any(i["path"] == "mods/new/thing.jar" for i in c.get("/api/servers/a/files/trash").json()["items"])
    assert c.get("/api/servers/a/files/download", params={"path": "config/a.toml"}).content == b"x = 1\n"
    z = zipfile.ZipFile(io.BytesIO(c.get("/api/servers/a/files/download", params=[("path", "world"), ("path", "config")]).content))
    assert sorted(z.namelist()) == ["config/a.toml", "world/region/r.0.0.mca"]
    assert z.read("world/region/r.0.0.mca") == b"\x00" * 5000


def test_compress_extract_search(c):
    assert c.post("/api/servers/a/files/compress", json={"paths": ["world", "config"], "name": "pack"}).json()["path"] == "pack.zip"
    r = c.post("/api/servers/a/files/extract", json={"path": "pack.zip"}).json()
    assert r == {"path": "pack", "files": 2} and (c.base / "pack" / "config" / "a.toml").exists()
    # an archive trying to write outside its folder
    with zipfile.ZipFile(c.base / "bad.zip", "w") as z:
        z.writestr("../../evil.txt", "x")
    assert c.post("/api/servers/a/files/extract", json={"path": "bad.zip"}).status_code == 400
    assert not (c.base.parent / "evil.txt").exists()
    with tarfile.open(c.base / "t.tar.gz", "w:gz") as t:
        t.add(c.base / "config", arcname="cfg")
    assert c.post("/api/servers/a/files/extract", json={"path": "t.tar.gz"}).json()["files"] == 1
    hits = c.get("/api/servers/a/files/search", params={"q": "a.TOML"}).json()["results"]
    assert {h["path"] for h in hits} == {"config/a.toml", "pack/config/a.toml", "t/cfg/a.toml"}
