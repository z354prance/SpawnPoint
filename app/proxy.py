"""Built-in Minecraft router.

Every server shares one port (25565). When a player connects, the first packet
(the handshake) contains the hostname they typed, e.g. atm10.mc.example.com. We read
it, look the server up, and then either:

- pipe the connection straight through to that server's container (online), or
- answer the server-list ping ourselves with a status line ("Asleep - join to
  wake it", "Generating spawn 62%"...), and on a join attempt start the server
  and tell the player to come back in a minute (wake-on-join).
"""
import asyncio
import ipaddress
import json
import logging
import socket
import struct

from . import config, dockerctl, proxyproto, state

log = logging.getLogger("spawnpoint.proxy")
MAX_HANDSHAKE = 1024


class ProtocolError(Exception):
    pass


# ------------------------------------------------------------------ wire helpers
def encode_varint(n: int) -> bytes:
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_varint(buf: bytes, pos: int = 0) -> tuple[int, int]:
    result = 0
    for i in range(5):
        if pos >= len(buf):
            raise IndexError
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            if result & 0x80000000:
                result -= 1 << 32
            return result, pos
    raise ProtocolError("varint too long")


def encode_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return encode_varint(len(b)) + b


def packet(pid: int, payload: bytes) -> bytes:
    body = encode_varint(pid) + payload
    return encode_varint(len(body)) + body


def parse_handshake(buf: bytes) -> tuple[dict, int] | None:
    """Return (handshake, bytes_consumed), or None if more bytes are needed."""
    try:
        length, pos = decode_varint(buf)
        if length <= 0 or length > MAX_HANDSHAKE:
            raise ProtocolError("bad handshake length")
        end = pos + length
        if len(buf) < end:
            return None
        pid, pos = decode_varint(buf, pos)
        if pid != 0:
            raise ProtocolError("not a handshake")
        proto, pos = decode_varint(buf, pos)
        slen, pos = decode_varint(buf, pos)
        host = buf[pos:pos + slen].decode("utf-8", "replace")
        pos += slen
        port = struct.unpack(">H", buf[pos:pos + 2])[0]
        pos += 2
        nxt, pos = decode_varint(buf, pos)
        return {"protocol": proto, "host": host, "port": port, "next": nxt}, end
    except IndexError:
        return None


def clean_host(raw: str) -> str:
    # Forge appends "\0FML\0" / "\0FML2\0" / "\0FML3\0"; some clients add a trailing dot.
    return raw.split("\x00", 1)[0].strip().rstrip(".").lower()


# ------------------------------------------------------------------ routing decisions
def lookup(host: str) -> dict | None:
    for s in state.servers().values():
        if s.get("hostname", "").lower() == host:
            return s
    return None


BUSY_PHASES = {"queued", "installing", "preparing", "pregen-spawn", "starting", "stopping"}


def describe(s: dict | None) -> tuple[str, str]:
    """(server-list MOTD, join-attempt message) for servers we can't forward to."""
    if s is None:
        return ("§cNo SpawnPoint server at this address",
                "There's no server at this address. Check the name and try again.")
    phase = s.get("phase")
    if phase == "pregen-spawn":
        pct = (s.get("pregen") or {}).get("percent") or 0
        return (f"§b⏳ Generating spawn… {pct:.0f}%",
                f"{s['name']} is generating its world ({pct:.0f}%). Try again in a minute or two.")
    if phase in BUSY_PHASES:
        return (f"§b⏳ {s['name']} is starting…", f"{s['name']} is starting up. Try again in about a minute.")
    if phase == "error":
        return ("§cServer error: the owner needs to check it", f"{s['name']} hit an error and needs attention.")
    if config.AUTO_WAKE:
        return (f"§d💤 {s['name']} is asleep: join to wake it",
                f"Waking {s['name']} up! Try again in about a minute.")
    return (f"§7{s['name']} is offline", f"{s['name']} is offline.")


async def _wake(s: dict) -> None:
    from . import servers
    try:
        await servers.start(s["id"])
        log.info("[%s] woken by a join attempt", s["id"])
    except Exception as e:
        log.warning("[%s] wake failed: %s", s["id"], e)


# ------------------------------------------------------------------ connection handling
async def _status_reply(reader, writer, hs: dict, motd: str, rest: bytes) -> None:
    status = {"version": {"name": "SpawnPoint", "protocol": hs["protocol"]},
              "players": {"max": 0, "online": 0},
              "description": {"text": motd}}
    buf = rest
    # Expect: status request (0x00), then ping (0x01 + long). Answer each.
    for _ in range(2):
        while True:
            try:
                length, pos = decode_varint(buf)
                if len(buf) >= pos + length:
                    break
            except IndexError:
                pass
            chunk = await asyncio.wait_for(reader.read(256), 10)
            if not chunk:
                return
            buf += chunk
        pid, p2 = decode_varint(buf, pos)
        body = buf[p2:pos + length]
        buf = buf[pos + length:]
        if pid == 0x00:
            writer.write(packet(0x00, encode_string(json.dumps(status))))
        elif pid == 0x01:
            writer.write(packet(0x01, body))
        await writer.drain()


def _kick(msg: str) -> bytes:
    return packet(0x00, encode_string(json.dumps({"text": msg})))


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


def _nodelay(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


def _nets() -> list:
    out = []
    for part in config.TRUSTED_PROXIES.split(","):
        try:
            out.append(ipaddress.ip_network(part.strip(), strict=False))
        except ValueError:
            pass
    return out


def trusted_proxy(ip: str) -> bool:
    """May this peer send a PROXY header? Only proxies and tunnels you run (TRUSTED_PROXIES)."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in n for n in _nets())


def backend_for(s: dict) -> tuple[str, int]:
    return dockerctl.addr(s["id"]), 25565


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    _nodelay(writer)
    peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
    client_ip, client_port = peer[0], peer[1]
    buf = b""
    # Anyone else connecting with a PROXY header just fails the handshake, so a player
    # can't claim someone else's address.
    header_done = not trusted_proxy(client_ip)
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(2048), 10)
            if not chunk:
                return
            buf += chunk
            if not header_done:
                # A reverse proxy or tunnel (Pangolin, nginx, Traefik) may prefix the connection
                # with a PROXY header carrying the player's real address. Strip it.
                pp = proxyproto.parse(buf)
                if pp is None:
                    continue
                used_pp, real = pp
                if real:
                    client_ip = real
                buf = buf[used_pp:]
                header_done = True
                if not buf:
                    continue
            if buf[0] == 0xFE:  # pre-1.7 legacy ping: not supported
                return
            if buf[:4] == b"AMMH":
                # AutoModpack fetching a server's mods: "AMMH", a 2-byte length, the host
                # the player typed. Route it by that host to the server's AutoModpack port.
                if len(buf) < 6 or len(buf) < 6 + struct.unpack(">H", buf[4:6])[0]:
                    continue
                n = struct.unpack(">H", buf[4:6])[0]
                await _automodpack(buf, clean_host(buf[6:6 + n].decode("utf-8", "replace")), reader, writer,
                                   client_ip, client_port)
                return
            parsed = parse_handshake(buf)
            if parsed:
                break
            if len(buf) > MAX_HANDSHAKE + 8:
                return
        hs, used = parsed
        host = clean_host(hs["host"])
        s = lookup(host)
        if s and s.get("phase") == "online":
            try:
                target = await asyncio.to_thread(backend_for, s)
                br, bw = await asyncio.wait_for(asyncio.open_connection(*target), 5)
            except (OSError, asyncio.TimeoutError):
                s = dict(s, phase="starting")  # container not reachable yet
            else:
                _nodelay(bw)
                if hs["next"] != 1:
                    log.info("player %s -> %s", client_ip, host)
                if s.get("real_ip"):
                    # The server was set up to read PROXY headers: tell it who this is.
                    bw.write(proxyproto.build_v2(client_ip, client_port, target[0], target[1]))
                bw.write(buf)  # replay the handshake (and anything after it) untouched
                await bw.drain()
                await asyncio.gather(_pipe(reader, bw), _pipe(br, writer))
                return
        if s is None:
            # Unknown address or a bare IP (what internet scanners send): say nothing,
            # so the router doesn't show up in public server lists.
            log.debug("ignored connection from %s for %r", client_ip, host)
            return
        motd, kick = describe(s)
        if hs["next"] == 1:
            await _status_reply(reader, writer, hs, motd, buf[used:])
        else:
            if s and config.AUTO_WAKE and s.get("phase") in ("stopped", "sleeping"):
                asyncio.create_task(_wake(s))
            writer.write(_kick(kick))
            await writer.drain()
    except (asyncio.TimeoutError, ProtocolError, proxyproto.ProxyError, ConnectionError, OSError, ValueError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _automodpack(buf: bytes, host: str, reader, writer, client_ip: str, client_port: int) -> None:
    from . import playerpack
    s = lookup(host)
    if not s or s.get("phase") != "online" or not (s.get("player_pack") or {}).get("sync"):
        return
    try:
        ip, _ = await asyncio.to_thread(backend_for, s)
        port = await asyncio.to_thread(playerpack.automodpack_port, dockerctl.server_dir(s["id"]))
        br, bw = await asyncio.wait_for(asyncio.open_connection(ip, port), 5)
    except (OSError, asyncio.TimeoutError):
        return
    _nodelay(bw)
    if s.get("real_ip"):
        # AutoModpack checks downloads against the player's address, which the game sees
        # through the same header, so both sides agree on who this is.
        bw.write(proxyproto.build_v2(client_ip, client_port, ip, port))
    bw.write(buf)
    await bw.drain()
    log.info("mods %s -> %s", client_ip, host)
    await asyncio.gather(_pipe(reader, bw), _pipe(br, writer))


async def serve() -> asyncio.base_events.Server:
    srv = await asyncio.start_server(handle, host="0.0.0.0", port=config.MC_PORT, reuse_address=True)
    log.info("Minecraft router listening on :%d", config.MC_PORT)
    return srv


# ------------------------------------------------------------------ real-IP self-check
async def _status_ok(target: tuple[str, int], header: bytes, host: str) -> bool:
    """Does the server answer a server-list ping (optionally preceded by a PROXY header)?"""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(*target), 5)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        hb = host.encode()
        hs = packet(0, encode_varint(767) + encode_varint(len(hb)) + hb + struct.pack(">H", 25565) + encode_varint(1))
        w.write(header + hs + packet(0, b""))
        await w.drain()
        data = await asyncio.wait_for(r.read(64), 6)
        return len(data) > 0
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        w.close()


async def verify_real_ip(sid: str) -> None:
    """Make sure the router talks to this server the way it actually accepts.
    If the real-IP mod didn't load, the server rejects our header and every player
    would be silently dropped, so fall back to plain connections (and the reverse)."""
    s = state.get(sid)
    try:
        target = await asyncio.to_thread(backend_for, s)
    except Exception:
        return
    hdr = proxyproto.build_v2("127.0.0.1", 1, target[0], target[1])
    with_hdr = await _status_ok(target, hdr, s["hostname"])
    plain = await _status_ok(target, b"", s["hostname"])
    if s.get("real_ip") and not with_hdr and plain:
        log.warning("[%s] server ignores real-IP headers (mod not loaded); sending plain connections", sid)
        state.update(sid, real_ip=False,
                     real_ip_via="the real-IP mod didn't load on this server, so it's off")
    elif not s.get("real_ip") and with_hdr and not plain:
        log.warning("[%s] server requires real-IP headers; turning them on", sid)
        state.update(sid, real_ip=True, real_ip_via="server requires it")
