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
        z.writestr("META-INF/MANIFEST.MF", "x")
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
    extra = tmp_path / "oculus.jar.disabled"; extra.write_bytes(b"x")
    playerpack.configure_automodpack(str(base), True, "https://sp/p/t", {"mods/oculus.jar": str(extra)})
    cfg = json.loads((base / "automodpack" / "automodpack-server.json").read_text())
    assert cfg["connection-mode"] == "MAGIC" and cfg["bind-port"] == 25580 and cfg["accept-proxy-protocol"] is True
    assert cfg["nag-clickable-link"] == "https://sp/p/t"
    assert os.listdir(base / "automodpack" / "host-modpack" / "main" / "mods") == ["oculus.jar"]
    cfg["other"] = 5
    (base / "automodpack" / "automodpack-server.json").write_text(json.dumps(cfg))
    playerpack.configure_automodpack(str(base), False, "", {})
    cfg = json.loads((base / "automodpack" / "automodpack-server.json").read_text())
    assert cfg["other"] == 5 and cfg["accept-proxy-protocol"] is False
    assert os.listdir(base / "automodpack" / "host-modpack" / "main" / "mods") == []


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
        monkeypatch.setattr(playerpack, "AUTOMODPACK_PORT", port)
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
