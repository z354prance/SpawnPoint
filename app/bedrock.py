"""Bedrock players (phones, consoles, Windows) via Geyser, per server.

Geyser translates Bedrock's protocol to Java inside the server; Floodgate lets Bedrock
players join without a Java account. Bedrock connects over UDP and doesn't say which
server address it wanted, so unlike Java players each Bedrock-enabled server gets its own
UDP port from BEDROCK_PORTS. SpawnPoint relays that port to the server's Geyser (19132
inside its container).

Availability depends on Geyser: Paper/Purpur 1.20.5 and newer; Fabric and NeoForge only on
the newest Minecraft release (Geyser has no builds for older modded versions).
"""
import asyncio
import logging
import os
import time

import httpx

from . import config, dockerctl, servers, state

log = logging.getLogger("spawnpoint.bedrock")
GEYSER_PORT = 19132          # inside the server's container
IDLE_S = 120                 # forget a Bedrock client after this long without packets
SUPPORTED = ("paper", "purpur", "fabric", "neoforge")
GEYSER_DL = "https://download.geysermc.org/v2/projects/{project}/versions/latest/builds/latest/downloads/{platform}"


def port_range() -> list[int]:
    a, _, b = config.BEDROCK_PORTS.partition("-")
    try:
        lo, hi = int(a), int(b or a)
    except ValueError:
        return []
    return list(range(lo, hi + 1))


def _free_port(sid: str) -> int | None:
    used = {(x.get("bedrock") or {}).get("port") for k, x in state.servers().items() if k != sid and (x.get("bedrock") or {}).get("enabled")}
    mine = ((state.get(sid) or {}).get("bedrock") or {}).get("port")
    if mine in port_range() and mine not in used:
        return mine
    return next((p for p in port_range() if p not in used), None)


# ------------------------------------------------------------------ UDP relay
class _Upstream(asyncio.DatagramProtocol):
    def __init__(self, relay: "_Relay", client):
        self.relay, self.client, self.transport, self.last = relay, client, None, time.monotonic()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.last = time.monotonic()
        if self.relay.transport:
            self.relay.transport.sendto(data, self.client)


class _Relay(asyncio.DatagramProtocol):
    """One public UDP port -> one server's Geyser. Each Bedrock client gets its own
    upstream socket, so replies find their way back."""

    def __init__(self, sid: str):
        self.sid, self.transport = sid, None
        self.clients: dict[tuple, _Upstream] = {}
        self.pending: dict[tuple, list] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        up = self.clients.get(addr)
        if up and up.transport:
            up.last = time.monotonic()
            up.transport.sendto(data)
            return
        if addr in self.pending:
            self.pending[addr].append(data)
            return
        self.pending[addr] = [data]
        asyncio.get_running_loop().create_task(self._open(addr))

    async def _open(self, addr):
        try:
            target = (await asyncio.to_thread(dockerctl.addr, self.sid), GEYSER_PORT)
            loop = asyncio.get_running_loop()
            _, up = await loop.create_datagram_endpoint(lambda: _Upstream(self, addr), remote_addr=target)
            self.clients[addr] = up
            for d in self.pending.pop(addr, []):
                up.transport.sendto(d)
        except Exception as e:
            log.debug("[%s] bedrock relay: %s", self.sid, e)
            self.pending.pop(addr, None)

    def sweep(self):
        now = time.monotonic()
        for addr, up in list(self.clients.items()):
            if now - up.last > IDLE_S:
                up.transport.close()
                del self.clients[addr]

    def close(self):
        for t in [up.transport for up in self.clients.values()] + [self.transport]:
            try:
                if t:
                    t.close()
            except RuntimeError:                         # its event loop is already gone (shutdown)
                pass


_relays: dict[str, tuple[int, _Relay]] = {}


async def start_relay(sid: str) -> None:
    b = (state.get(sid) or {}).get("bedrock") or {}
    port = b.get("port")
    if not b.get("enabled") or not port:
        return
    cur = _relays.get(sid)
    if cur and cur[0] == port:
        return
    stop_relay(sid)
    loop = asyncio.get_running_loop()
    _, relay = await loop.create_datagram_endpoint(lambda: _Relay(sid), local_addr=("0.0.0.0", port))
    _relays[sid] = (port, relay)
    log.info("[%s] Bedrock players on UDP %d", sid, port)


def stop_relay(sid: str) -> None:
    cur = _relays.pop(sid, None)
    if cur:
        cur[1].close()


async def start_all() -> None:
    for sid, s in list(state.servers().items()):
        if (s.get("bedrock") or {}).get("enabled"):
            try:
                await start_relay(sid)
            except OSError as e:
                log.warning("[%s] Bedrock port %s unavailable: %s", sid, (s.get("bedrock") or {}).get("port"), e)


async def sweep_loop() -> None:
    while True:
        await asyncio.sleep(30)
        for _, relay in list(_relays.values()):
            relay.sweep()


def stop_all() -> None:
    for sid in list(_relays):
        stop_relay(sid)


# ------------------------------------------------------------------ turning it on and off
async def _geyser_download(s: dict, project: str) -> str:
    """Paper/Purpur: GeyserMC's own download, when Modrinth has no build (Floodgate)."""
    d = servers.mods_dir(s)
    os.makedirs(d, exist_ok=True)
    name = f"{project}-spigot.jar"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(GEYSER_DL.format(project=project, platform="spigot"))
        r.raise_for_status()
    with open(os.path.join(d, name), "wb") as f:
        f.write(r.content)
    return name


async def enable(sid: str) -> dict:
    s = state.get(sid)
    if s.get("loader") not in SUPPORTED:
        raise ValueError("Bedrock players need a Paper, Purpur, Fabric or NeoForge server (Geyser doesn't run on "
                         f"{s.get('loader') or 'this server type'})")
    port = _free_port(sid)
    if port is None:
        raise ValueError(f"All Bedrock ports ({config.BEDROCK_PORTS}) are in use: turn Bedrock off on another server or widen BEDROCK_PORTS")
    added, notes = [], []
    try:
        r = await servers.install_mod(s, "geyser")
        added.append({"project": "geyser", "file": r.get("filename"), "managed": r.get("managed"), "project_id": r.get("project_id")})
    except ValueError:
        raise ValueError(f"Geyser has no build for {s['loader']} {s.get('mc_version')}. On Fabric and NeoForge it only "
                         "supports the newest Minecraft release; on Paper and Purpur, 1.20.5 and newer.")
    try:
        r = await servers.install_mod(state.get(sid), "floodgate")
        added.append({"project": "floodgate", "file": r.get("filename"), "managed": r.get("managed"), "project_id": r.get("project_id")})
    except Exception:
        if s["loader"] in ("paper", "purpur"):
            try:
                added.append({"project": "floodgate", "file": await _geyser_download(s, "floodgate"), "managed": False})
            except Exception as e:
                notes.append(f"Floodgate couldn't be added ({e}), so Bedrock players need a Java account")
        else:
            notes.append("Floodgate isn't available for this version, so Bedrock players need a Java account")
    state.update(sid, bedrock={"enabled": True, "port": port, "files": added, "note": "; ".join(notes)}, restart_pending=True)
    try:
        await start_relay(sid)
    except OSError as e:
        notes.append(f"UDP port {port} couldn't be opened: {e}")
        state.update(sid, bedrock={**state.get(sid)["bedrock"], "note": "; ".join(notes)})
    return state.get(sid)["bedrock"]


async def disable(sid: str) -> dict:
    s = state.get(sid)
    b = dict(s.get("bedrock") or {})
    for f in b.get("files") or []:
        if f.get("managed") and f.get("project_id"):
            servers.remove_managed_mod(sid, f["project_id"])
        if f.get("file"):
            p = os.path.join(servers.mods_dir(s), os.path.basename(f["file"]))
            if os.path.exists(p):
                os.remove(p)
    stop_relay(sid)
    b.update(enabled=False, files=[], note="")
    state.update(sid, bedrock=b, restart_pending=True)
    return b
