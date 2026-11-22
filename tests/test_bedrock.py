import asyncio
import os
import socket

import pytest

from app import bedrock, config, dockerctl, servers, state


@pytest.fixture
def srv(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    state.put({"id": "p", "name": "p", "loader": "paper", "mc_version": "1.21.4", "hostname": "p.mc.example.com"})
    yield
    bedrock.stop_all()


def _free_udp_ports(n):
    socks = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def test_relay_round_trip(srv, monkeypatch):
    """A Bedrock client's packets reach the server's Geyser and the answers come back."""
    pub, geyser = _free_udp_ports(2)
    monkeypatch.setattr(bedrock, "GEYSER_PORT", geyser)
    monkeypatch.setattr(dockerctl, "addr", lambda sid: "127.0.0.1")
    state.update("p", bedrock={"enabled": True, "port": pub})

    class Echo(asyncio.DatagramProtocol):
        def connection_made(self, t): self.t = t
        def datagram_received(self, d, a): self.t.sendto(b"pong:" + d, a)

    async def go():
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(Echo, local_addr=("127.0.0.1", geyser))
        await bedrock.start_relay("p")
        got = []

        class Client(asyncio.DatagramProtocol):
            def datagram_received(self, d, a): got.append(d)
        t, _ = await loop.create_datagram_endpoint(Client, remote_addr=("127.0.0.1", pub))
        t.sendto(b"ping1"); await asyncio.sleep(0.2); t.sendto(b"ping2"); await asyncio.sleep(0.2)
        bedrock.stop_relay("p")
        return got
    assert asyncio.run(go()) == [b"pong:ping1", b"pong:ping2"]


def test_ports_are_shared_out(srv, monkeypatch):
    monkeypatch.setattr(config, "BEDROCK_PORTS", "30001-30002")
    state.put({"id": "q", "name": "q", "loader": "paper", "bedrock": {"enabled": True, "port": 30001}})
    assert bedrock._free_port("p") == 30002
    state.put({"id": "r", "name": "r", "loader": "paper", "bedrock": {"enabled": True, "port": 30002}})
    assert bedrock._free_port("p") is None


def test_enable_and_disable(srv, monkeypatch):
    monkeypatch.setattr(config, "BEDROCK_PORTS", "30011-30012")
    monkeypatch.setattr(bedrock, "start_relay", lambda sid: asyncio.sleep(0))
    calls = []

    async def install(s, project):
        calls.append(project)
        if project == "floodgate":
            raise ValueError("no build")
        return {"filename": f"{project}.jar", "managed": True, "project_id": "id-" + project}
    async def dl(s, project):
        os.makedirs(servers.mods_dir(s), exist_ok=True)
        open(os.path.join(servers.mods_dir(s), "floodgate-spigot.jar"), "w").close()
        return "floodgate-spigot.jar"
    monkeypatch.setattr(servers, "install_mod", install)
    monkeypatch.setattr(bedrock, "_geyser_download", dl)
    b = asyncio.run(bedrock.enable("p"))
    assert b["enabled"] and b["port"] == 30011 and calls == ["geyser", "floodgate"]
    assert [f["project"] for f in b["files"]] == ["geyser", "floodgate"] and state.get("p")["restart_pending"]
    state.update("p", managed_mods=[{"project_id": "id-geyser"}])
    b = asyncio.run(bedrock.disable("p"))
    assert not b["enabled"] and state.get("p")["managed_mods"] == []
    assert not os.path.exists(os.path.join(servers.mods_dir(state.get("p")), "floodgate-spigot.jar"))


def test_unsupported_server_types(srv, monkeypatch):
    state.put({"id": "f", "name": "f", "loader": "forge", "mc_version": "1.20.1"})
    with pytest.raises(ValueError, match="Paper, Purpur, Fabric or NeoForge"):
        asyncio.run(bedrock.enable("f"))

    async def none(s, project):
        raise ValueError("no build")
    monkeypatch.setattr(servers, "install_mod", none)
    state.put({"id": "n", "name": "n", "loader": "neoforge", "mc_version": "1.21.1"})
    with pytest.raises(ValueError, match="newest Minecraft release"):
        asyncio.run(bedrock.enable("n"))
