"""CI: a real NeoForge server with AutoModpack answers SpawnPoint's AMMH routing.

Usage: ci_automodpack_check.py <server dir> <container ip> configure <real_ip 0|1>
       ci_automodpack_check.py <server dir> <container ip> check <real_ip 0|1>"""
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import playerpack, proxy, proxyproto  # noqa: E402

base, ip, what, real = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
HOST = b"pack.mc.example.com"
HELLO = b"AMMH" + struct.pack(">H", len(HOST)) + HOST


async def ask(port: int, header: bytes = b"") -> bytes:
    r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1" if port != playerpack.AUTOMODPACK_PORT else ip, port), 10)
    w.write(header + HELLO)
    await w.drain()
    try:
        return await asyncio.wait_for(r.readexactly(4), 10)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
        return getattr(e, "partial", b"") or b""
    finally:
        w.close()


async def check() -> None:
    hdr = proxyproto.build_v2("203.0.113.9", 4444, ip, playerpack.AUTOMODPACK_PORT) if real else b""
    direct = await ask(playerpack.AUTOMODPACK_PORT, hdr)
    print("direct:", direct)
    assert direct == b"AMOK", f"AutoModpack didn't answer on its port: {direct!r}"
    proxy.lookup = lambda h: {"id": "x", "phase": "online", "real_ip": real, "player_pack": {"sync": True}} if h == HOST.decode() else None
    proxy.backend_for = lambda s: (ip, 25565)
    front = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    via = await ask(front.sockets[0].getsockname()[1])
    front.close()
    print("through the router:", via)
    assert via == b"AMOK", f"The router didn't reach AutoModpack: {via!r}"
    if real:
        bare = await ask(playerpack.AUTOMODPACK_PORT)          # with the header required, a bare connection is refused
        print("without the header:", bare)
        assert bare != b"AMOK"


if what == "configure":
    playerpack.configure_automodpack(base, real, "https://sp.example.com/p/token", {})
    print(open(os.path.join(base, "automodpack", "automodpack-server.json")).read())
else:
    asyncio.run(check())
    print("ok")
