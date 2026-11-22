import pytest
import asyncio
import json
import os

from app import catalog, config, dockerctl, proxy, proxyproto, realip, state
from tests.test_proxy import handshake


def test_parse_v1_v2_and_none():
    assert proxyproto.parse(b"PROXY TCP4 203.0.113.7 10.0.0.69 51234 25565\r\nrest") == (46, "203.0.113.7")
    assert proxyproto.parse(b"PROXY UNKNOWN\r\n") == (15, None)
    assert proxyproto.parse(b"PROX") is None                     # need more bytes
    hdr = proxyproto.build_v2("2001:db8::5", 4000, "10.0.0.69", 25565)
    assert proxyproto.parse(hdr + b"x") == (len(hdr), "2001:db8::5")
    assert proxyproto.parse(hdr[:10]) is None
    assert proxyproto.parse(handshake("a.mc.example.com", 2)) == (0, None)  # plain Minecraft


def _setup(monkeypatch, tmp_path, srv):
    monkeypatch.setattr(state, "_state", {"servers": {srv["id"]: srv}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))


def _run_router(monkeypatch, client_bytes):
    async def run():
        got = {}
        async def backend(r, w):
            got["data"] = await r.read(4096)
            w.close()
        b = await asyncio.start_server(backend, "127.0.0.1", 0)
        bport = b.sockets[0].getsockname()[1]
        monkeypatch.setattr(proxy, "backend_for", lambda s: ("127.0.0.1", bport))
        srv = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", srv.sockets[0].getsockname()[1])
        w.write(client_bytes)
        await w.drain()
        await asyncio.sleep(0.3)
        w.close(); srv.close(); b.close()
        return got["data"]
    return asyncio.run(run())


def test_router_passes_real_ip(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, {"id": "smp", "name": "smp", "hostname": "smp.mc.example.com",
                                   "phase": "online", "real_ip": True})
    hs = handshake("smp.mc.example.com", 2) + b"login"
    data = _run_router(monkeypatch, b"PROXY TCP4 198.51.100.23 10.0.0.69 40000 25565\r\n" + hs)
    used, ip = proxyproto.parse(data)
    assert ip == "198.51.100.23" and data[used:] == hs   # Pangolin's header in, ours out


def test_router_no_header_when_not_supported(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, {"id": "v", "name": "v", "hostname": "v.mc.example.com",
                                   "phase": "online", "real_ip": False})
    hs = handshake("v.mc.example.com", 2) + b"login"
    data = _run_router(monkeypatch, b"PROXY TCP4 198.51.100.23 10.0.0.69 40000 25565\r\n" + hs)
    assert data == hs                                     # stripped, nothing added


def test_ensure_paper_and_mods(monkeypatch, tmp_path):
    monkeypatch.setattr(dockerctl, "trusted_sources", lambda: ["172.18.0.0/16", "172.18.0.1"])
    paper = {"id": "p", "name": "p", "loader": "paper", "mc_version": "1.21.1"}
    neo = {"id": "n", "name": "n", "loader": "neoforge", "mc_version": "1.21.1"}
    vanilla = {"id": "va", "name": "va", "loader": "vanilla", "mc_version": "1.21.1"}
    monkeypatch.setattr(state, "_state", {"servers": {"p": paper, "n": neo, "va": vanilla}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    os.makedirs(tmp_path / "p" / "config")
    (tmp_path / "p" / "config" / "paper-global.yml").write_text("proxies:\n  proxy-protocol: false\n")
    async def fake_dl(slug, loader, ver, dest):
        os.makedirs(dest, exist_ok=True)
        open(os.path.join(dest, f"{slug}-1.0.1.jar"), "w").close()
        return {"filename": f"{slug}-1.0.1.jar"}
    monkeypatch.setattr(catalog, "download_mod", fake_dl)
    assert asyncio.run(realip.ensure(paper)) and "proxy-protocol: true" in (tmp_path / "p/config/paper-global.yml").read_text()
    assert asyncio.run(realip.ensure(neo))
    cfg = json.loads((tmp_path / "n/config/haproxycompat.json").read_text())
    assert cfg["trustedProxies"] == ["172.18.0.0/16", "172.18.0.1"] and cfg["requireProxyProtocol"]
    assert 'trusted_proxies = ["172.18.0.0/16", "172.18.0.1"]' in (tmp_path / "n/config/haproxycompat-common.toml").read_text()
    assert state.get("n")["real_ip_via"] == "haproxycompat"
    assert not asyncio.run(realip.ensure(vanilla)) and state.get("va")["real_ip"] is False


def test_untrusted_peers_cant_send_proxy_headers(monkeypatch, tmp_path):
    from app import config
    assert proxy.trusted_proxy("192.168.1.10") and proxy.trusted_proxy("::ffff:10.0.0.5")
    assert not proxy.trusted_proxy("203.0.113.9")
    _setup(monkeypatch, tmp_path, {"id": "smp", "name": "smp", "hostname": "smp.mc.example.com",
                                   "phase": "online", "real_ip": True})
    monkeypatch.setattr(config, "TRUSTED_PROXIES", "10.0.0.0/8")          # the test client (127.0.0.1) isn't a proxy
    hs = handshake("smp.mc.example.com", 2) + b"login"
    with pytest.raises(KeyError):                                        # refused: nothing reaches the server
        _run_router(monkeypatch, b"PROXY TCP4 198.51.100.23 10.0.0.69 40000 25565\r\n" + hs)
    data = _run_router(monkeypatch, hs)                                  # a normal direct connection works
    used, ip = proxyproto.parse(data)
    assert ip == "127.0.0.1" and data[used:] == hs
