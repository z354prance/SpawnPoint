import os
import socket
import struct
import threading

import pytest

os.environ.setdefault("DATA_DIR", "/tmp/by-test/servers")
os.environ.setdefault("BACKUP_DIR", "/tmp/by-test/backups")

from app import perf, rcon, servers, topology  # noqa: E402


def make_sysfs(tmp_path, groups):
    for g in groups:
        for cpu in g:
            d = tmp_path / f"cpu{cpu}" / "cache" / "index3"
            d.mkdir(parents=True, exist_ok=True)
            (d / "shared_cpu_list").write_text(",".join(str(c) for c in g) + "\n")
    return str(tmp_path)


def epyc_7f72_groups():
    # 24 cores / 48 threads, 2 cores per L3 -> 12 groups of 4 logical CPUs
    return [[2 * i, 2 * i + 1, 2 * i + 24, 2 * i + 25] for i in range(12)]


def test_parse_cpu_list():
    assert topology.parse_cpu_list("0-3,8,10-11") == [0, 1, 2, 3, 8, 10, 11]
    assert topology.format_cpu_list([3, 1, 1, 2]) == "1,2,3"


def test_detect_groups(tmp_path):
    groups = topology.detect_groups(make_sysfs(tmp_path, epyc_7f72_groups()))
    assert len(groups) == 12 and groups[0] == [0, 1, 24, 25]


def test_allocator_home_and_burst():
    a = topology.CoreAllocator(epyc_7f72_groups(), reserved=2)
    h1 = a.assign_home("atm", 2)
    h2 = a.assign_home("smp", 1)
    assert not set(h1) & set(h2) and 0 not in h1 + h2 and 1 not in h1 + h2
    b = a.lend_burst("atm", 3, max_servers=2)
    assert len(b) == 3 and not set(b) & set(h1 + h2)
    assert len(a.cpus_for("atm")) == 20 and len(a.cpus_for("atm", include_burst=False)) == 8
    a.return_burst("atm")
    assert len(a.cpus_for("atm")) == 8
    # Oversubscription: more servers than groups shares the least-loaded group
    for i in range(12):
        a.assign_home(f"s{i}", 1)
    assert all(a.home[f"s{i}"] for i in range(12))


def test_java_tags():
    assert perf.java_tag("1.21.1") == "java21"
    assert perf.java_tag("1.20.4") == "java17"
    assert perf.java_tag("1.20.5") == "java21"
    assert perf.java_tag("1.16.5") == "java8"
    assert perf.java_tag("26.1") == "java25"


def test_memory_and_flags():
    assert perf.memory_plan("paper") == (2, 6, 12)
    assert perf.memory_plan("modrinth", None) == (3, 12, 24)
    assert perf.memory_plan("curseforge", 172) == (4, 12, 24)        # aoc
    assert perf.memory_plan("modrinth", 400) == (4, 16, 32)
    assert perf.memory_plan("fabric", 20) == (2, 8, 16)
    assert "-XX:SoftMaxHeapSize=12g" in perf.jvm_opts("java21", 8, 12)
    assert "SoftMaxHeapSize" not in perf.jvm_opts("java17", 8, 12)   # G1 ignores it
    assert "MaxHeapFreeRatio" in perf.jvm_opts("java17", 8, 12)
    f21 = perf.jvm_opts("java21", 20)
    assert "-XX:+UseZGC" in f21 and "-XX:+ZGenerational" in f21 and "-Dmax.bg.threads=20" in f21
    assert "ZGenerational" not in perf.jvm_opts("java25", 4)
    assert "G1PeriodicGCInterval" in perf.jvm_opts("java17", 4) and perf.use_aikar("java17")


def test_parsers():
    assert rcon.parse_players("There are 2 of a max of 20 players online: Steve, Alex") == (2, 20, ["Steve", "Alex"])
    assert rcon.parse_players("There are 0 of a max of 20 players online:") == (0, 20, [])
    assert rcon.parse_mspt("Target tick rate: 20.0 per second.\nAverage time per tick: 12.3ms (Target: 50.0ms)") == 12.3
    assert rcon.parse_mspt("Tick durations (min/med/95%ile/max ms) from last 10s, 1m:\n 1.2/3.4/5.6/7.8; 1/2/3/4") == 3.4


def test_rcon_roundtrip():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        def read():
            ln = struct.unpack("<i", c.recv(4))[0]
            d = c.recv(ln)
            return struct.unpack("<ii", d[:8]) + (d[8:-2].decode(),)
        rid, _, pw = read()
        c.sendall(rcon._packet(rid if pw == "pw" else -1, 2, ""))
        rid, _, cmd = read()
        c.sendall(rcon._packet(rid, 0, f"§aYou said {cmd}"))
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    assert rcon.command("127.0.0.1", "pw", "list", port=port) == "You said list"


def test_properties_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(servers.dockerctl, "server_dir", lambda sid: str(tmp_path))
    (tmp_path / "server.properties").write_text("#c\nmotd=Hi\nview-distance=10\n")
    servers.write_properties("x", {"view-distance": "8", "white-list": "true"})
    assert dict(servers.read_properties("x")) == {"motd": "Hi", "view-distance": "8", "white-list": "true"}


def test_detect_loader(tmp_path):
    (tmp_path / "libraries/net/neoforged").mkdir(parents=True)
    (tmp_path / "libraries/net/minecraft/server/1.21.1-20240808.144430").mkdir(parents=True)
    assert servers.detect_loader_version(str(tmp_path)) == ("neoforge", "1.21.1")


def test_build_env():
    from app import dockerctl
    s = {"id": "atm10", "name": "atm10", "kind": "curseforge", "java_tag": "java21", "rcon_password": "x",
         "memory": {"init_gb": 4, "max_gb": 12}, "pack": {"slug": "all-the-mods-10"}, "first_boot": True}
    env = dockerctl.build_env(s, 20)
    assert env["TYPE"] == "AUTO_CURSEFORGE" and env["CF_SLUG"] == "all-the-mods-10"
    assert env["MAX_MEMORY"] == "12G" and env["SKIP_SERVER_PROPERTIES"] == "false"
    assert "-XX:SoftMaxHeapSize=12g" in env["JVM_OPTS"]   # old records: soft = max
    s["first_boot"] = False
    assert dockerctl.build_env(s, 20)["SKIP_SERVER_PROPERTIES"] == "true"


def test_host_path(monkeypatch):
    from app import dockerctl, config
    monkeypatch.setattr(dockerctl, "_mounts", [("/servers", "/mnt/nvme/minecraft/servers"), ("/backups", "/mnt/user/backups/mc")])
    monkeypatch.setattr(config, "DATA_HOST_DIR", "")
    monkeypatch.setattr(config, "ISOLATED", False)  # shared mode: translate to host paths
    assert dockerctl.host_path("/servers/atm10") == "/mnt/nvme/minecraft/servers/atm10"
    assert dockerctl.host_path("/serversX/a") == "/serversX/a"
    monkeypatch.setattr(config, "DATA_HOST_DIR", "/mnt/fast/mc")
    monkeypatch.setattr(config, "DATA_DIR", "/servers")
    assert dockerctl.host_path("/servers/atm10") == "/mnt/fast/mc/atm10"


def test_addr_modes(monkeypatch):
    from app import dockerctl, config

    class C:
        id = "abc"
        status = "running"
        attrs = {"NetworkSettings": {"Networks": {"mcnet": {"IPAddress": "172.30.0.5"}}}}
        def reload(self): pass
    monkeypatch.setattr(dockerctl, "get_container", lambda sid: C())
    monkeypatch.setattr(config, "ISOLATED", False)
    assert dockerctl.addr("atm10") == "mc-atm10"
    monkeypatch.setattr(config, "ISOLATED", True)
    dockerctl._addr_cache.clear()
    assert dockerctl.addr("atm10") == "172.30.0.5"
    assert dockerctl.host_path("/servers/atm10") == "/servers/atm10"


def test_routes_not_shadowed():
    """Each POST endpoint must reach its own handler (a catch-all once swallowed /command)."""
    from fastapi.routing import APIRoute
    from app.main import app
    paths = [r.path for r in app.routes if isinstance(r, APIRoute) and "POST" in r.methods]
    generic = [p for p in paths if p.endswith("/{action}")]
    assert generic == ["/api/servers/{sid}/action/{action}"]


def test_secret_key_persists(tmp_path, monkeypatch):
    from app import config
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    k1 = config._load_secret_key()
    k2 = config._load_secret_key()
    assert k1 == k2 and len(k1) == 64
    assert oct((tmp_path / "secret.key").stat().st_mode & 0o777) == "0o600"


def test_rcon_reuses_one_connection_and_reconnects():
    """Several commands share one connection (Minecraft logs each new one);
    if the server drops it, the next command reconnects."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    accepts = []

    def serve():
        for round_ in range(2):
            c, _ = srv.accept()
            accepts.append(1)
            def read():
                ln = struct.unpack("<i", c.recv(4))[0]
                d = b""
                while len(d) < ln:
                    d += c.recv(ln - len(d))
                return struct.unpack("<ii", d[:8]) + (d[8:-2].decode(),)
            rid, _, pw = read()
            c.sendall(rcon._packet(rid, 2, ""))
            for _ in range(3 if round_ == 0 else 1):
                rid, _, cmd = read()
                c.sendall(rcon._packet(rid, 0, f"ok {cmd}"))
            c.close()  # server goes away after round 0

    threading.Thread(target=serve, daemon=True).start()
    rcon.drop("127.0.0.1")
    assert [rcon.command("127.0.0.1", "pw", f"c{i}", port=port) for i in range(3)] == ["ok c0", "ok c1", "ok c2"]
    assert len(accepts) == 1
    assert rcon.command("127.0.0.1", "pw", "again", port=port) == "ok again"
    assert len(accepts) == 2
    rcon.drop("127.0.0.1")


def test_container_memory_headroom():
    assert perf.container_limit_gb(6) == 9            # small server: +3 GB
    assert perf.container_limit_gb(12, 172) == 17     # big pack: +35% (aoc: was 14)
    assert perf.container_limit_gb(16, 400) == 22


def _desktop_sysfs(tmp_path, cores=6, smt=True):
    """One L3 shared by every core, like a Ryzen 5 or a Core i5."""
    import os
    n = cores * (2 if smt else 1)
    allc = f"0-{n - 1}"
    for c in range(n):
        d = tmp_path / f"cpu{c}"
        os.makedirs(d / "cache" / "index3"); os.makedirs(d / "topology")
        (d / "cache" / "index3" / "shared_cpu_list").write_text(allc)
        sib = f"{c % cores},{c % cores + cores}" if smt else str(c)
        (d / "topology" / "thread_siblings_list").write_text(sib)
    return str(tmp_path)


def test_desktop_cpu_is_split_into_units(tmp_path):
    g = topology.detect_groups(_desktop_sysfs(tmp_path, cores=6))
    assert g == [[0, 1, 6, 7], [2, 3, 8, 9], [4, 5, 10, 11]]      # 2 cores + their SMT threads each
    assert topology.reserved_count(len(g), 2) == 1                # Minecraft keeps 2 of 3 units
    a = topology.CoreAllocator(g, topology.reserved_count(len(g), 2))
    assert a.cpus_for("x") == [] and a.assign_home("x", 2) == [1, 2]


def test_small_cpus_keep_cores_for_minecraft():
    assert topology.reserved_count(12, 2) == 2     # 7F72: as configured
    assert topology.reserved_count(2, 2) == 0      # 4-core desktop: nothing held back
    assert topology.reserved_count(4, 2) == 1


def test_ram_pool_follows_host(monkeypatch):
    from app import config, perf
    monkeypatch.setattr(config, "_RAM_POOL_SET", "")
    for host, pool in ((256, 96), (64, 48), (32, 24), (16, 12), (8, 4)):
        monkeypatch.setattr(config, "_host_gb", float(host))
        assert config.ram_pool_gb() == pool, host
    monkeypatch.setattr(config, "_host_gb", 16.0)
    start, normal, ceiling = perf.memory_plan("curseforge", 320)  # a huge pack on a 16 GB machine
    assert ceiling <= 9 and normal <= ceiling and start <= normal
    assert perf.container_limit_gb(ceiling, 320) <= 14
    monkeypatch.setattr(config, "_host_gb", 256.0)
    assert perf.memory_plan("curseforge", 320) == (4, 16, 32)     # the big box is unchanged
    monkeypatch.setattr(config, "_RAM_POOL_SET", "40"); monkeypatch.setattr(config, "RAM_POOL_GB", 40)
    assert config.ram_pool_gb() == 40                             # an explicit setting wins


def test_page_asks_for_current_scripts():
    from fastapi.testclient import TestClient
    from app import main
    r = TestClient(main.app).get("/")
    assert r.headers["cache-control"] == "no-cache"
    import re
    v = re.findall(r'/static/app\.js\?v=([0-9a-f]+)', r.text)
    assert v and f'/static/style.css?v={v[0]}' in r.text
