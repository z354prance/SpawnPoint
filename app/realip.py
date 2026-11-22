"""Make each Minecraft server accept PROXY headers from SpawnPoint, so it sees real
player IPs. Only servers that are set up get a header (a server that isn't expecting
one would reject the connection).

- Paper / Purpur: built in (`proxies.proxy-protocol` in config/paper-global.yml).
- NeoForge (1.21+) and Fabric: the HAProxyCompat mod.
- Fabric/Quilt (older) and Forge 1.20.1: Proxy Protocol Support / NG HAProxy Support.
- Vanilla or anything else without a matching mod: no header, the server sees
  SpawnPoint's address as before.

Each mod only trusts headers from the addresses we list: SpawnPoint's private
network, which only SpawnPoint and the game servers are on.
"""
import json
import logging
import os
import re

from . import dockerctl, state

log = logging.getLogger("spawnpoint.realip")

MODS = {  # loader -> Modrinth slugs to try, in order
    "neoforge": ["haproxycompat"],
    "fabric": ["haproxycompat", "proxy-protocol-support"],
    "quilt": ["proxy-protocol-support"],
    "forge": ["ng-haproxy-support"],
}
MARKERS = ("haproxycompat", "proxy-protocol-support", "proxy_protocol_support", "ng-haproxy", "ngproxy")


def _mods_dir(s: dict) -> str:
    return os.path.join(dockerctl.server_dir(s["id"]), "mods")


def _installed(s: dict) -> str | None:
    d = _mods_dir(s)
    if not os.path.isdir(d):
        return None
    for f in os.listdir(d):
        low = f.lower()
        if low.endswith(".jar") and any(m in low for m in MARKERS):
            return low
    return None


def _write_mod_configs(sdir: str, trusted: list[str]) -> None:
    cfg = os.path.join(sdir, "config")
    os.makedirs(cfg, exist_ok=True)
    # HAProxyCompat (Fabric JSON + NeoForge TOML)
    with open(os.path.join(cfg, "haproxycompat.json"), "w") as f:
        json.dump({"enabled": True, "requireProxyProtocol": True, "trustedProxies": trusted,
                   "logConnections": False,
                   "kickMessage": "Please join through the server address, not its IP."}, f, indent=2)
    toml_list = ", ".join(f'"{t}"' for t in trusted)
    with open(os.path.join(cfg, "haproxycompat-common.toml"), "w") as f:
        f.write("[general]\nenabled = true\nrequire_proxy_protocol = true\n"
                f"trusted_proxies = [{toml_list}]\nlog_connections = false\n"
                'kick_message = "Please join through the server address, not its IP."\n')
    # Proxy Protocol Support (Fabric/Quilt) and NG HAProxy Support (Forge) share this file
    with open(os.path.join(cfg, "proxy_protocol_support.json"), "w") as f:
        json.dump({"enableProxyProtocol": True, "proxyServerIPs": trusted, "directAccessIPs": [],
                   "whitelistTCPShieldServers": False}, f, indent=2)


def _paper(sdir: str) -> bool:
    import yaml
    p = os.path.join(sdir, "config", "paper-global.yml")
    if not os.path.exists(p):
        return False
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("proxies", {})["proxy-protocol"] = True
    with open(p, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return True


async def ensure(s: dict) -> bool:
    """Set the server up for real IPs if possible. Call while the server is stopped.
    Records the result in state as real_ip (True/False) and real_ip_via."""
    sid, sdir, loader = s["id"], dockerctl.server_dir(s["id"]), s.get("loader") or ""
    if s.get("real_ip_disabled"):
        state.update(sid, real_ip=False, real_ip_via="turned off")
        return False
    via = None
    try:
        if loader in ("paper", "purpur"):
            if _paper(sdir):
                via = "Paper proxy-protocol"
        elif loader in MODS:
            from . import servers
            jar = next((m["filename"].lower() for m in s.get("managed_mods", [])
                        if any(k in m["slug"] for k in MARKERS)), None) \
                or (None if servers.is_managed(s) else _installed(s))
            if not jar:
                for slug in MODS[loader]:
                    try:
                        r = await servers.install_mod(state.get(sid), slug)
                        jar = r["filename"].lower()
                        break
                    except Exception as e:
                        log.info("[%s] %s not available for %s %s: %s", sid, slug, loader, s.get("mc_version"), e)
            if jar:
                trusted = dockerctl.trusted_sources()
                _write_mod_configs(sdir, trusted)
                via = re.sub(r"[-_]?\d.*$", "", jar.replace(".jar", "")) or jar
    except Exception as e:
        log.warning("[%s] real-IP setup failed: %s", sid, e)
        via = None
    state.update(sid, real_ip=bool(via), real_ip_via=via or "not supported for this server type")
    return bool(via)
