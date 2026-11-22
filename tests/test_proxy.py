import asyncio
import json
import struct

from app import config, proxy, state


def handshake(host, nxt, proto=767, port=25565):
    body = proxy.encode_varint(0) + proxy.encode_varint(proto) + proxy.encode_string(host) + struct.pack(">H", port) + proxy.encode_varint(nxt)
    return proxy.encode_varint(len(body)) + body


_left: dict = {}


async def read_packet(r):
    buf = _left.pop(id(r), b"")
    while True:
        try:
            ln, pos = proxy.decode_varint(buf)
            if len(buf) >= pos + ln:
                pid, p2 = proxy.decode_varint(buf, pos)
                _left[id(r)] = buf[pos + ln:]
                return pid, buf[p2:pos + ln]
        except IndexError:
            pass
        chunk = await asyncio.wait_for(r.read(4096), 3)
        if not chunk:
            raise EOFError
        buf += chunk


def read_string(b):
    n, pos = proxy.decode_varint(b)
    return b[pos:pos + n].decode()


def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 25565, 2**31 - 1, -1):
        assert proxy.decode_varint(proxy.encode_varint(n))[0] == n


def test_parse_handshake_forge_host():
    hs, used = proxy.parse_handshake(handshake("ATM10.mc.example.com\x00FML3\x00", 2) + b"extra")
    assert proxy.clean_host(hs["host"]) == "atm10.mc.example.com" and hs["next"] == 2
    assert proxy.parse_handshake(handshake("a.b", 1)[:5]) is None


def _setup(monkeypatch, tmp_path, servers_):
    monkeypatch.setattr(state, "_state", {"servers": servers_})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(config, "MC_PORT", 0)


def test_status_and_wake(monkeypatch, tmp_path):
    s = {"id": "atm10", "name": "atm10", "hostname": "atm10.mc.example.com", "phase": "sleeping"}
    _setup(monkeypatch, tmp_path, {"atm10": s})
    woke = []
    async def fake_wake(x): woke.append(x["id"])
    monkeypatch.setattr(proxy, "_wake", fake_wake)

    async def run():
        srv = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        # server-list ping
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(handshake("atm10.mc.example.com", 1) + proxy.packet(0, b"") + proxy.packet(1, b"12345678"))
        pid, body = await read_packet(r)
        status = json.loads(read_string(body))
        pid2, body2 = await read_packet(r)
        w.close()
        # join attempt
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(handshake("atm10.mc.example.com", 2))
        kpid, kbody = await read_packet(r)
        w.close()
        # unknown host / bare IP (scanners): connection closed with no reply at all
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(handshake("203.0.113.9", 1) + proxy.packet(0, b""))
        ubody = await asyncio.wait_for(r.read(4096), 3)
        srv.close()
        await asyncio.sleep(0.05)
        return status, (pid2, body2), json.loads(read_string(kbody)), ubody

    status, pong, kick, unknown = asyncio.run(run())
    assert "asleep" in status["description"]["text"] and status["version"]["protocol"] == 767
    assert pong == (1, b"12345678")
    assert "Waking atm10" in kick["text"] and woke == ["atm10"]
    assert unknown == b""


def test_forwarding(monkeypatch, tmp_path):
    s = {"id": "smp", "name": "smp", "hostname": "smp.mc.example.com", "phase": "online"}
    _setup(monkeypatch, tmp_path, {"smp": s})

    async def run():
        got = {}
        async def backend(r, w):
            got["first"] = await r.read(4096)
            w.write(b"hello-from-server")
            await w.drain()
            got["more"] = await r.read(4096)
            w.close()
        b = await asyncio.start_server(backend, "127.0.0.1", 0)
        bport = b.sockets[0].getsockname()[1]
        monkeypatch.setattr(proxy, "backend_for", lambda s: ("127.0.0.1", bport))
        srv = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        hs = handshake("SMP.mc.example.com", 2) + b"login-start"
        w.write(hs)
        reply = await asyncio.wait_for(r.read(4096), 3)
        w.write(b"gameplay")
        await w.drain()
        await asyncio.sleep(0.1)
        w.close()
        srv.close(); b.close()
        return hs, got, reply

    hs, got, reply = asyncio.run(run())
    assert got["first"] == hs            # handshake replayed byte-for-byte
    assert got["more"] == b"gameplay"
    assert reply == b"hello-from-server"
