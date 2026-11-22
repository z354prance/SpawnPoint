"""HAProxy PROXY protocol (v1 and v2).

In:  Pangolin (Traefik) can put a PROXY header in front of each connection with the
     player's real address. We detect and strip it automatically; connections
     without one (LAN players, older setups) keep working.
Out: for servers set up to accept it, we send a v2 header to the Minecraft server
     so it sees the real player IP in logs, bans and plugins.
"""
import ipaddress
import socket
import struct

V2_SIG = b"\r\n\r\n\x00\r\nQUIT\n"
V1_PREFIX = b"PROXY "
MAX_V1 = 107


class ProxyError(Exception):
    pass


def parse(buf: bytes) -> tuple[int, str | None] | None:
    """Look for a PROXY header at the start of buf.

    Returns (bytes_consumed, client_ip_or_None), (0, None) when there's no header,
    or None when more bytes are needed to decide.
    """
    if not buf:
        return None
    # v2
    if buf[0] == 0x0D:
        if len(buf) < 16:
            return None if V2_SIG.startswith(buf[:12]) else (0, None)
        if buf[:12] != V2_SIG:
            return 0, None
        ver_cmd, fam, length = buf[12], buf[13], struct.unpack(">H", buf[14:16])[0]
        if ver_cmd >> 4 != 2:
            raise ProxyError("bad PROXY v2 version")
        end = 16 + length
        if len(buf) < end:
            return None
        if ver_cmd & 0x0F == 0:  # LOCAL (health checks etc.)
            return end, None
        body = buf[16:end]
        af = fam >> 4
        if af == 1 and len(body) >= 12:
            return end, socket.inet_ntop(socket.AF_INET, body[0:4])
        if af == 2 and len(body) >= 36:
            return end, socket.inet_ntop(socket.AF_INET6, body[0:16])
        return end, None
    # v1
    if buf[0] == ord("P"):
        if len(buf) < len(V1_PREFIX):
            return None if V1_PREFIX.startswith(buf) else (0, None)
        if not buf.startswith(V1_PREFIX):
            return 0, None
        nl = buf.find(b"\r\n", 0, MAX_V1)
        if nl < 0:
            if len(buf) >= MAX_V1:
                raise ProxyError("PROXY v1 line too long")
            return None
        parts = buf[:nl].decode("ascii", "replace").split(" ")
        if len(parts) >= 3 and parts[1] in ("TCP4", "TCP6"):
            try:
                ipaddress.ip_address(parts[2])
                return nl + 2, parts[2]
            except ValueError:
                raise ProxyError("bad PROXY v1 address")
        return nl + 2, None  # "PROXY UNKNOWN"
    return 0, None


def build_v2(src_ip: str, src_port: int, dst_ip: str, dst_port: int) -> bytes:
    """PROXY v2 header (PROXY command, TCP) announcing src -> dst."""
    src = ipaddress.ip_address(src_ip)
    try:
        dst = ipaddress.ip_address(dst_ip)
    except ValueError:
        dst = ipaddress.ip_address("0.0.0.0" if src.version == 4 else "::")
    if src.version != dst.version:  # header needs one family: map the destination
        dst = ipaddress.ip_address("0.0.0.0" if src.version == 4 else "::")
    if src.version == 4:
        fam, addr = 0x11, src.packed + dst.packed + struct.pack(">HH", src_port, dst_port)
    else:
        fam, addr = 0x21, src.packed + dst.packed + struct.pack(">HH", src_port, dst_port)
    return V2_SIG + bytes([0x21, fam]) + struct.pack(">H", len(addr)) + addr
