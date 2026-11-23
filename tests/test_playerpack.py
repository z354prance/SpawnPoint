import asyncio
import hashlib
import io
import json
import os
import struct
import zipfile

from fastapi.testclient import TestClient

from app import config, dockerctl, main, playerpack, proxy, proxyproto, servers, state, users


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
    assert 'PreLaunchCommand="$INST_JAVA" -jar packwiz-installer-bootstrap.jar https://sp.example.com/p/tok/pack/pack.toml' in cfg
    comps = json.loads(z.read("mmc-pack.json"))["components"]
    assert {"uid": "net.neoforged", "version": "21.1.200"} in comps
    assert z.read(".minecraft/packwiz-installer-bootstrap.jar") == b"JAR"
    assert b"a.mc.example.com" in z.read(".minecraft/servers.dat")


def test_automodpack_config(tmp_path):
    base = tmp_path / "srv"
    cfgp = base / "automodpack" / "automodpack-server.json"
    extra = tmp_path / "oculus.jar.disabled"; extra.write_bytes(b"x")
    playerpack.configure_automodpack(str(base), True, "https://sp/p/t", {})
    assert not (base / "automodpack").exists()                  # AutoModpack makes it on its first start
    # 4.x: shares the game's port
    cfgp.parent.mkdir(parents=True, exist_ok=True)
    cfgp.write_text(json.dumps({"bindPort": 1234, "nagClickableLink": "x", "validateSecrets": True, "bind-port": 25580}))
    playerpack.configure_automodpack(str(base), True, "https://sp/p/t", {"mods/oculus.jar": str(extra)})
    cfg = json.loads(cfgp.read_text())
    assert cfg["bindPort"] == -1 and cfg["nagClickableLink"] == "https://sp/p/t" and cfg["validateSecrets"]
    assert "bind-port" not in cfg and playerpack.automodpack_port(str(base)) == 25565
    assert os.listdir(base / "automodpack" / "host-modpack" / "main" / "mods") == ["oculus.jar"]
    # newer releases: their own port
    cfgp.write_text(json.dumps({"connection-mode": "OFF", "bind-port": -1, "other": 5}))
    playerpack.configure_automodpack(str(base), False, "", {})
    cfg = json.loads(cfgp.read_text())
    assert cfg["connection-mode"] == "MAGIC" and cfg["bind-port"] == 25580 and cfg["accept-proxy-protocol"] is False
    assert cfg["other"] == 5 and playerpack.automodpack_port(str(base)) == 25580
    assert os.listdir(base / "automodpack" / "host-modpack" / "main") == []    # nothing extra any more


def test_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(playerpack, "bootstrap_jar", lambda: b"JAR")
    _server(tmp_path, monkeypatch)
    users._fails.clear(); users.ensure_bootstrap()
    c = TestClient(main.app)
    assert c.put("/api/servers/a/player-pack", json={"enabled": True}).status_code == 401
    c.post("/api/login", json={"username": "admin", "password": "pw"})
    assert c.put("/api/servers/a/player-pack", json={"enabled": True, "base_url": "not a url"}).status_code == 400
    r = c.put("/api/servers/a/player-pack", json={"enabled": True, "base_url": "https://sp.example.com/"}).json()
    assert r["enabled"] and r["mods"] == 2 and r["page"] == f"https://sp.example.com/p/{r['token']}"
    t = r["token"]
    anon = TestClient(main.app)
    assert anon.get(f"/p/{t}").status_code == 200 and anon.get("/p/wrongtoken123").status_code == 404
    info = anon.get(f"/p/{t}/info").json()
    assert info["mods"] == 2 and info["address"] == "a.mc.example.com"
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


def test_router_sends_ammh_to_automodpack(tmp_path, monkeypatch):
    """A connection starting with AMMH goes to the server's AutoModpack port, whole, with a
    PROXY header when real IPs are on."""
    got = {}

    async def run():
        async def backend(r, w):
            data = b""
            while not data.endswith(b"hello"):
                data += await r.read(4096)
            got["data"] = data
            w.write(b"AMOK"); await w.drain(); w.close()
        srv = await asyncio.start_server(backend, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        monkeypatch.setattr(playerpack, "automodpack_port", lambda base: port)
        monkeypatch.setattr(proxy, "backend_for", lambda s: ("127.0.0.1", 25565))
        monkeypatch.setattr(proxy, "lookup", lambda h: {"id": "a", "phase": "online", "real_ip": True,
                                                       "player_pack": {"sync": True}} if h == "a.mc.example.com" else None)
        front = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        fport = front.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", fport)
        host = b"a.mc.example.com"
        w.write(b"AMMH" + struct.pack(">H", len(host)) + host + b"hello")
        await w.drain()
        reply = await asyncio.wait_for(r.read(4), 5)
        w.close(); srv.close(); front.close()
        return reply

    assert asyncio.run(run()) == b"AMOK"
    data = got["data"]
    assert data.startswith(proxyproto.SIG if hasattr(proxyproto, "SIG") else b"\r\n\r\n\x00\r\nQUIT\n")
    used, ip = proxyproto.parse(data)
    assert ip == "127.0.0.1" and data[used:].startswith(b"AMMH") and data.endswith(b"hello")


def _with_amp(tmp_path, monkeypatch):
    base, s = _server(tmp_path, monkeypatch)
    _jar(base / "mods" / "automodpack-mc1.21.1-neoforge-4.0.7.jar")
    playerpack.set_settings("a", on=True, sync=True)
    return base, state.get("a")


def test_automodpack_in_every_download(tmp_path, monkeypatch):
    base, s = _with_amp(tmp_path, monkeypatch)
    _, idx, fl, _ = playerpack.index(s)
    assert b"automodpack-mc1.21.1-neoforge-4.0.7.jar" in idx          # Prism's packwiz list
    # the any-launcher zip is just AutoModpack (it fetches the rest)
    names = [n for n, _ in playerpack.plain_zip_files(s)]
    assert names == ["mods/automodpack-mc1.21.1-neoforge-4.0.7.jar", "servers.dat", "READ ME.txt"]
    # AutoModpack hands out the checked client files and the client-only mods
    extra = playerpack.client_only_extras(s)
    assert set(extra) == {"mods/oculus-1.8.jar", "config/jei/jei.toml"}
    playerpack.before_boot(s)
    assert not (base / "automodpack").exists()                  # not before AutoModpack's first start
    (base / "automodpack").mkdir()
    (base / "automodpack" / "automodpack-server.json").write_text('{"bindPort": -1}')
    playerpack.before_boot(s)
    host = base / "automodpack" / "host-modpack" / "main"
    assert (host / "config" / "jei" / "jei.toml").exists() and (host / "mods" / "oculus-1.8.jar").exists()
    assert not (host / "config" / "discord.toml").exists()
    # with sync off: the full zip, and no AutoModpack anywhere
    playerpack.set_settings("a", sync=False)
    s = state.get("a")
    assert "mods/create-1.21.1-6.0.jar" in [n for n, _ in playerpack.plain_zip_files(s)]
    assert b"automodpack" not in playerpack.index(s)[1]


def test_mrpack(tmp_path, monkeypatch):
    base, s = _with_amp(tmp_path, monkeypatch)
    create = base / "mods" / "create-1.21.1-6.0.jar"
    sha1 = hashlib.sha1(create.read_bytes()).hexdigest()

    async def lookup(hashes):
        return {h: ({"url": "https://cdn.modrinth.com/data/x/create.jar"} if h == sha1 else None) for h in hashes}
    monkeypatch.setattr(playerpack, "modrinth_lookup", lookup)
    # AutoModpack running: just AutoModpack, with the loader for the Modrinth App to set up
    z = zipfile.ZipFile(asyncio.run(playerpack.mrpack(s)))
    meta = json.loads(z.read("modrinth.index.json"))
    assert meta["dependencies"] == {"minecraft": "1.21.1", "neoforge": "21.1.200"} and meta["files"] == []
    assert sorted(z.namelist()) == ["modrinth.index.json", "overrides/mods/automodpack-mc1.21.1-neoforge-4.0.7.jar", "overrides/servers.dat"]
    # without AutoModpack: everything, with Modrinth's own downloads where they exist
    playerpack.set_settings("a", sync=False)
    s = state.get("a")
    path = asyncio.run(playerpack.mrpack(s))
    z = zipfile.ZipFile(path)
    meta = json.loads(z.read("modrinth.index.json"))
    assert [f["path"] for f in meta["files"]] == ["mods/create-1.21.1-6.0.jar"]
    f = meta["files"][0]
    assert f["downloads"] == ["https://cdn.modrinth.com/data/x/create.jar"] and f["hashes"]["sha1"] == sha1
    assert len(f["hashes"]["sha512"]) == 128 and f["fileSize"] == create.stat().st_size
    inside = set(z.namelist())
    assert {"overrides/mods/oculus-1.8.jar", "overrides/config/jei/jei.toml", "overrides/servers.dat"} <= inside
    assert not any("automodpack" in n for n in inside)
    assert "overrides/mods/create-1.21.1-6.0.jar" not in inside and "overrides/config/discord.toml" not in inside
    assert asyncio.run(playerpack.mrpack(s)) == path                  # unchanged files: same build


def test_sync_waits_for_the_jar(tmp_path, monkeypatch):
    """Turned on but not downloaded yet (needs a restart): full downloads, and the card says so."""
    _, s = _server(tmp_path, monkeypatch)
    playerpack.set_settings("a", on=True, sync=True)
    s = state.get("a")
    assert not playerpack.sync_active(s)
    assert "mods/create-1.21.1-6.0.jar" in [n for n, _ in playerpack.plain_zip_files(s)]
    assert main._pack_view("a")["sync_pending"] is True


def test_repin_test_build(tmp_path, monkeypatch):
    base, s = _with_amp(tmp_path, monkeypatch)
    rc = base / "mods" / "automodpack-mc1.21.1-neoforge-5.0.0-rc.7.jar"
    os.rename(base / "mods" / "automodpack-mc1.21.1-neoforge-4.0.7.jar", rc)
    state.update("a", managed_mods=[{"project_id": "k68glP2e", "slug": "automodpack", "version": "5.0.0-rc.7",
                                     "filename": rc.name, "version_id": "x", "title": "AutoModpack"}])

    async def install(s, project):
        return {"version": "4.0.7", "filename": "automodpack-mc1.21.1-neoforge-4.0.7.jar"}
    monkeypatch.setattr(servers, "install_mod", install)
    asyncio.run(playerpack.repin(state.get("a")))
    assert not rc.exists()


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
