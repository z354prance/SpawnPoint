"""Minimal Source RCON client (what Minecraft speaks) plus output parsers."""
import re
import socket
import struct
import threading

_COLOR = re.compile(r"§.")


class RconError(Exception):
    pass


def _packet(req_id: int, ptype: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RconError("connection closed")
        buf += chunk
    return buf


class _Conn:
    """One open RCON connection to a server. Minecraft logs every new RCON connection,
    so reusing one keeps the server console free of connect/disconnect spam."""

    def __init__(self, host: str, port: int, password: str, timeout: float):
        self.lock = threading.Lock()
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.next_id = 1
        self._send(3, password)
        if self._recv_id() == -1:
            self.close()
            raise RconError("bad RCON password")

    def _send(self, ptype: int, body: str) -> int:
        rid = self.next_id
        self.next_id = self.next_id % 2_000_000_000 + 1
        self.sock.sendall(_packet(rid, ptype, body))
        return rid

    def _recv(self) -> tuple[int, bytes]:
        ln = struct.unpack("<i", _recv_exact(self.sock, 4))[0]
        data = _recv_exact(self.sock, ln)
        return struct.unpack("<i", data[:4])[0], data

    def _recv_id(self) -> int:
        return self._recv()[0]

    def run(self, cmd: str) -> str:
        rid = self._send(2, cmd)
        while True:
            got, data = self._recv()
            if got == rid:
                return _COLOR.sub("", data[8:-2].decode("utf-8", "replace"))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


_conns: dict[tuple, _Conn] = {}
_conns_lock = threading.Lock()


def command(host: str, password: str, cmd: str, port: int = 25575, timeout: float = 10) -> str:
    key = (host, port, password)
    for attempt in (1, 2):  # a kept connection may have gone stale (server restarted)
        with _conns_lock:
            conn = _conns.get(key)
        try:
            if conn is None:
                conn = _Conn(host, port, password, timeout)
                with _conns_lock:
                    _conns[key] = conn
            with conn.lock:
                return conn.run(cmd)
        except RconError as e:
            drop(host)
            if "password" in str(e) or attempt == 2:
                raise
        except OSError as e:
            drop(host)
            if attempt == 2:
                raise RconError(str(e)) from e
    raise RconError("unreachable")


def drop(host: str) -> None:
    """Close kept connections to a host (server stopped or recreated)."""
    with _conns_lock:
        for key in [k for k in _conns if k[0] == host]:
            _conns.pop(key).close()


def parse_players(out: str) -> tuple[int, int, list[str]]:
    m = re.search(r"There are (\d+)\D+?(\d+) players online:?\s*(.*)", out, re.S)
    if not m:
        return 0, 0, []
    names = [n.strip() for n in m.group(3).split(",") if n.strip()]
    return int(m.group(1)), int(m.group(2)), names


def parse_tick(out: str) -> dict | None:
    """Average and 95th-percentile tick time from 'tick query' (1.20.3+) or 'spark tps'.
    The 95th percentile catches the occasional long ticks players feel as rubberbanding,
    which an average hides."""
    m = re.search(r"Average time per tick:\s*([\d.]+)\s*ms", out)
    if m:
        p95 = re.search(r"P95:\s*([\d.]+)\s*ms", out)
        avg = float(m.group(1))
        return {"avg": avg, "p95": float(p95.group(1)) if p95 else avg}
    m = re.search(r"Tick durations.*?:\s*([\d.]+)/([\d.]+)/([\d.]+)/", out, re.S)
    if m:
        return {"avg": float(m.group(2)), "p95": float(m.group(3))}
    # Forge / NeoForge: "Overall : Mean tick time: 7.746 ms. Mean TPS: 20.000" (no percentiles)
    m = re.search(r"Overall\s*:\s*Mean tick time:\s*([\d.]+)\s*ms", out)
    if m:
        return {"avg": float(m.group(1)), "p95": float(m.group(1))}
    return None


def parse_mspt(out: str) -> float | None:
    """Parse 'tick query' (vanilla 1.20.3+) or 'spark tps' output to average ms per tick."""
    m = re.search(r"Average time per tick:\s*([\d.]+)\s*ms", out)
    if m:
        return float(m.group(1))
    # spark: "Tick durations (min/med/95%ile/max ms) from last 10s, 1m:\n 1.2/3.4/5.6/7.8; ..."
    m = re.search(r"Tick durations.*?:\s*([\d.]+)/([\d.]+)/", out, re.S)
    if m:
        return float(m.group(2))
    return None
