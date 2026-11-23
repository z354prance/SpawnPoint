import asyncio
import hashlib
import io
import json
import os
import zipfile

from fastapi.testclient import TestClient

from app import config, dockerctl, main, playerpack, servers, state, users


def _jar(path, fabric_env=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", str(path))
        if fabric_env:
            z.writestr("fabric.mod.json", json.dumps({"id": "m", "environment": fabric_env}))


def _server(tmp_path, monkeypatch, **extra):
    base = tmp_path / "srv"
    (base / "mods").mkdir(parents=True)
    (base / "config" / "jei").mkdir(parents=True)
    (base / "kubejs" / "server_scripts").mkdir(parents=True)
    _jar(base / "mods" / "create-1.21.1-6.0.jar")
    _jar(base / "mods" / "Chunky-NeoForge-1.4.jar")
    _jar(base / "mods" / "lithium-server.jar", fabric_env="server")
    _jar(base / "mods" / "oculus-1.8.jar.disabled")
    (base / "config" / "jei" / "jei.toml").write_text("a = 1\n")
    (base / "config" / "discord.toml").write_text('token = "abcdefghijklmnopqrstuvwxyz123"\n')
    (base / "kubejs" / "server_scripts" / "x.js").write_text("//")
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(base))
    monkeypatch.setattr(servers, "mods_dir", lambda s: str(base / "mods"))
    s = {"id": "a", "name": "My Pack", "phase": "online", "loader": "neoforge", "loader_version": "21.1.200",
         "mc_version": "1.21.1", "hostname": "a.mc.example.com",
         "auto_disabled": [{"mod_id": "oculus", "name": "Oculus", "file": "oculus-1.8.jar"}], **extra}
    state.put(s)
    return base, state.get("a")


def test_files_and_packwiz_index(tmp_path, monkeypatch):
    base, s = _server(tmp_path, monkeypatch)
    fl, left = playerpack.files(s)
    assert set(fl) == {"mods/create-1.21.1-6.0.jar", "mods/oculus-1.8.jar", "config/jei/jei.toml"}
    assert any("Chunky" in x for x in left) and any("lithium" in x for x in left)
    assert any("discord.toml" in x and "password" in x for x in left)
    pack, idx, _, _ = playerpack.index(s)
    assert b'hash-format = "sha256"' in idx and b'file = "mods/oculus-1.8.jar"' in idx
    want = hashlib.sha256((base / "mods" / "create-1.21.1-6.0.jar").read_bytes()).hexdigest()
    assert want.encode() in idx
    assert idx.count(b"preserve = true") == 1                       # the config, not the mods
    assert hashlib.sha256(idx).hexdigest().encode() in pack
    assert b'neoforge = "21.1.200"' in pack and b'minecraft = "1.21.1"' in pack


def test_servers_dat_is_valid_nbt(tmp_path):
    from app import flatmap
    raw = playerpack._servers_dat("My Pack", "a.mc.example.com")
    tag = flatmap.read_nbt(raw) if hasattr(flatmap, "read_nbt") else None
    if tag is not None:
        assert tag["servers"][0]["ip"] == "a.mc.example.com"
    assert raw[:1] == b"\x0a" and b"a.mc.example.com" in raw


def test_prism_zip(tmp_path, monkeypatch):
    _, s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(playerpack, "bootstrap_jar", lambda: b"JAR")
    z = zipfile.ZipFile(io.BytesIO(playerpack.prism_zip(s, "https://sp.example.com/p/tok/pack/pack.toml")))
    cfg = z.read("instance.cfg").decode()
    assert "OverrideCommands=true" in cfg
    assert 'PreLaunchCommand="\\"$INST_JAVA\\" -jar packwiz-installer-bootstrap.jar https://sp.example.com/p/tok/pack/pack.toml"' in cfg
    comps = json.loads(z.read("mmc-pack.json"))["components"]
    assert {"uid": "net.neoforged", "version": "21.1.200"} in comps
    assert z.read(".minecraft/packwiz-installer-bootstrap.jar") == b"JAR"
    assert b"a.mc.example.com" in z.read(".minecraft/servers.dat")


def test_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")

    async def links():
        return {"page": "https://github.com/x/y/releases/latest", "windows": "https://github.com/x/y/a-setup.exe"}
    monkeypatch.setattr(playerpack, "app_downloads", links)
    monkeypatch.setattr(playerpack, "bootstrap_jar", lambda: b"JAR")
    _server(tmp_path, monkeypatch)
    users._fails.clear(); users.ensure_bootstrap()
    c = TestClient(main.app)
    assert c.put("/api/servers/a/player-pack", json={"enabled": True}).status_code == 401
    c.post("/api/login", json={"username": "admin", "password": "pw"})
    assert c.put("/api/servers/a/player-pack", json={"enabled": True, "base_url": "not a url"}).status_code == 400
    r = c.put("/api/servers/a/player-pack", json={"enabled": True, "base_url": "https://sp.example.com/"}).json()
    assert r["enabled"] and r["page"] == f"https://sp.example.com/p/{r['token']}"
    assert c.get("/api/servers/a/player-pack").json()["mods"] == 2
    t = r["token"]
    fast = c.get("/api/servers/a/player-pack", params={"details": False}).json()
    assert fast["page"] == r["page"] and "mods" not in fast            # answers without scanning the pack
    anon = TestClient(main.app)
    assert anon.get(f"/p/{t}").status_code == 200 and anon.get("/p/wrongtoken123").status_code == 404
    info = anon.get(f"/p/{t}/info").json()
    assert info["mods"] == 2 and info["address"] == "a.mc.example.com"
    assert info["app"]["windows"].endswith("-setup.exe") and info["page"].endswith(f"/p/{t}")
    assert b"index.toml" in anon.get(f"/p/{t}/pack/pack.toml").content
    assert anon.get(f"/p/{t}/pack/mods/create-1.21.1-6.0.jar").status_code == 200
    for bad in ("config/discord.toml", "mods/Chunky-NeoForge-1.4.jar", "../srv/config/jei/jei.toml", "kubejs/server_scripts/x.js"):
        assert anon.get(f"/p/{t}/pack/{bad}").status_code == 404, bad
    z = zipfile.ZipFile(io.BytesIO(anon.get(f"/p/{t}/prism.zip").content))
    assert f"https://sp.example.com/p/{t}/pack/pack.toml" in z.read("instance.cfg").decode()
    z = zipfile.ZipFile(io.BytesIO(anon.get(f"/p/{t}/pack.zip").content))
    assert "mods/oculus-1.8.jar" in z.namelist() and "READ ME.txt" in z.namelist()

    async def nothing(hashes):
        return {h: None for h in hashes}
    monkeypatch.setattr(playerpack, "modrinth_lookup", nothing)
    z = zipfile.ZipFile(io.BytesIO(anon.get(f"/p/{t}/pack.mrpack").content))
    assert "modrinth.index.json" in z.namelist() and "overrides/mods/create-1.21.1-6.0.jar" in z.namelist()
    t2 = c.put("/api/servers/a/player-pack", json={"new_link": True}).json()["token"]
    assert t2 != t and anon.get(f"/p/{t}/info").status_code == 404
    c.put("/api/servers/a/player-pack", json={"enabled": False})
    assert anon.get(f"/p/{t2}/info").status_code == 404


def test_catalog_prefers_stable(monkeypatch):
    from app import catalog
    import httpx
    vs = [{"id": "rc", "project_id": "p", "version_number": "5.0.0-rc.7", "version_type": "beta", "files": [{"filename": "a-rc.jar", "primary": True}]},
          {"id": "ok", "project_id": "p", "version_number": "4.0.7", "version_type": "release", "files": [{"filename": "a.jar", "primary": True}]}]

    def handler(req):
        return httpx.Response(200, json=vs if req.url.path.endswith("/version") else {"slug": "a", "title": "A"})
    real = httpx.AsyncClient
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(catalog.find_version("a", "neoforge", "1.21.1"))["version"] == "4.0.7"


def test_loader_version_from_libraries(tmp_path, monkeypatch):
    """Modpacks don't tell SpawnPoint the loader version: it's read from the libraries,
    never the Minecraft version (Prism can't find "NeoForge 1.21.1")."""
    base, _ = _server(tmp_path, monkeypatch, loader_version="")
    (base / "libraries" / "net" / "neoforged" / "neoforge" / "21.1.209").mkdir(parents=True)
    (base / "libraries" / "net" / "minecraft" / "server" / "1.21.1-20240808.144430").mkdir(parents=True)
    assert playerpack.loader_info(state.get("a")) == ("neoforge", "21.1.209")
    state.update("a", loader_version="1.21.1")                  # the wrong value an earlier version could store
    assert playerpack.loader_info(state.get("a")) == ("neoforge", "21.1.209")


def test_on_by_default_for_new_modded_servers(tmp_path, monkeypatch):
    state.put({"id": "m", "name": "M", "kind": "ftb", "created": 1})
    state.put({"id": "v", "name": "V", "kind": "vanilla", "created": 2})
    playerpack.turn_on_for_new("m", "http://192.168.1.5:8090")     # a local address players couldn't reach
    pp = state.get("m")["player_pack"]
    assert pp["enabled"] and len(pp["token"]) >= 12 and pp["base_url"] == ""
    playerpack.turn_on_for_new("v", "https://sp.example.com")
    assert "player_pack" not in state.get("v")                      # plain servers have no mods to hand out
    state.update("m", player_pack={**pp, "base_url": "https://sp.example.com"})
    state.put({"id": "n", "name": "N", "kind": "neoforge", "created": 3})
    playerpack.turn_on_for_new("n", "http://10.0.0.2:8090")
    assert state.get("n")["player_pack"]["base_url"] == "https://sp.example.com"   # reuses the known address
    state.put({"id": "o", "name": "O", "kind": "modrinth", "created": 4})
    state.update("m", player_pack={})
    state.update("n", player_pack={})
    playerpack.turn_on_for_new("o", "https://spawnpoint.example.org")
    assert state.get("o")["player_pack"]["base_url"] == "https://spawnpoint.example.org"


def test_mrpack(tmp_path, monkeypatch):
    base, s = _server(tmp_path, monkeypatch)
    create = base / "mods" / "create-1.21.1-6.0.jar"
    sha1 = hashlib.sha1(create.read_bytes()).hexdigest()

    async def lookup(hashes):
        return {h: ({"url": "https://cdn.modrinth.com/data/x/create.jar"} if h == sha1 else None) for h in hashes}
    monkeypatch.setattr(playerpack, "modrinth_lookup", lookup)
    path = asyncio.run(playerpack.mrpack(s))
    z = zipfile.ZipFile(path)
    meta = json.loads(z.read("modrinth.index.json"))
    assert meta["dependencies"] == {"minecraft": "1.21.1", "neoforge": "21.1.200"}
    assert [f["path"] for f in meta["files"]] == ["mods/create-1.21.1-6.0.jar"]
    f = meta["files"][0]
    assert f["downloads"] == ["https://cdn.modrinth.com/data/x/create.jar"] and f["hashes"]["sha1"] == sha1
    assert len(f["hashes"]["sha512"]) == 128 and f["fileSize"] == create.stat().st_size
    inside = set(z.namelist())
    assert {"overrides/mods/oculus-1.8.jar", "overrides/config/jei/jei.toml", "overrides/servers.dat"} <= inside
    assert "overrides/mods/create-1.21.1-6.0.jar" not in inside and "overrides/config/discord.toml" not in inside
    assert asyncio.run(playerpack.mrpack(s)) == path                  # unchanged files: same build


def test_old_automodpack_is_removed(tmp_path, monkeypatch):
    """Servers that had AutoModpack turned on by an earlier version lose it at their next start."""
    base, _ = _server(tmp_path, monkeypatch)
    _jar(base / "mods" / "automodpack-mc1.21.1-neoforge-4.0.7.jar")
    (base / "automodpack" / "host-modpack" / "main" / "config").mkdir(parents=True)
    removed = []
    monkeypatch.setattr(servers, "remove_managed_mod", lambda sid, pid: removed.append(pid))
    state.update("a", player_pack={"enabled": True, "token": "tok12345", "sync": True},
                 managed_mods=[{"project_id": "k68glP2e", "slug": "automodpack"}])
    playerpack.before_boot(state.get("a"))
    assert removed == ["k68glP2e"] and not (base / "automodpack").exists()
    assert not any(f.startswith("automodpack") for f in os.listdir(base / "mods"))
    assert state.get("a")["player_pack"] == {"enabled": True, "token": "tok12345"}
    _jar(base / "mods" / "automodpack-own.jar")                      # never touched without the old setting
    playerpack.before_boot(state.get("a"))
    assert (base / "mods" / "automodpack-own.jar").exists()



def test_sync_for_the_companion_app(tmp_path, monkeypatch):
    import tarfile
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    base, _ = _server(tmp_path, monkeypatch)
    playerpack.set_settings("a", on=True)
    t = state.get("a")["player_pack"]["token"]
    anon = TestClient(main.app)
    idx = anon.get(f"/p/{t}/sync").json()["files"]
    assert set(idx) == {"mods/create-1.21.1-6.0.jar", "mods/oculus-1.8.jar", "config/jei/jei.toml"}
    want = hashlib.sha256((base / "config" / "jei" / "jei.toml").read_bytes()).hexdigest()
    assert idx["config/jei/jei.toml"] == {"sha256": want, "size": 6, "preserve": True}
    assert idx["mods/oculus-1.8.jar"]["preserve"] is False
    r = anon.post(f"/p/{t}/sync", json={"files": ["mods/oculus-1.8.jar", "config/jei/jei.toml",
                                                    "config/discord.toml", "../../etc/passwd"]})
    assert r.headers["x-sync-files"] == "2"
    tf = tarfile.open(fileobj=io.BytesIO(r.content))
    assert sorted(tf.getnames()) == ["config/jei/jei.toml", "mods/oculus-1.8.jar"]   # only pack files
    assert tf.extractfile("config/jei/jei.toml").read() == b"a = 1\n"
    assert int(r.headers["x-sync-bytes"]) == sum(m.size for m in tf.getmembers())
