"""Docker side: build/replace server containers, log streaming, stats, thread pinning."""
import collections
import logging
import os
import socket
import threading
import time

import docker
from docker.errors import NotFound

from . import config, perf, topology

log = logging.getLogger("spawnpoint.docker")
_client = None


def client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def cname(sid: str) -> str:
    return f"mc-{sid}"


def server_dir(sid: str) -> str:
    return os.path.join(config.DATA_DIR, sid)


_mounts: list[tuple[str, str]] | None = None


def _own_mounts() -> list[tuple[str, str]]:
    """(container_path, host_path) pairs for SpawnPoint's own bind mounts, longest first."""
    global _mounts
    if _mounts is None:
        _mounts = []
        try:
            me = client().containers.get(socket.gethostname())
            for m in me.attrs.get("Mounts", []):
                if m.get("Type") == "bind":
                    _mounts.append((m["Destination"].rstrip("/"), m["Source"].rstrip("/")))
        except Exception as e:
            log.warning("could not read own mounts (%s); assuming host paths equal container paths", e)
        _mounts.sort(key=lambda x: len(x[0]), reverse=True)
    return _mounts


_addr_cache: dict[str, tuple[str, str]] = {}


def addr(sid: str) -> str:
    """Where SpawnPoint reaches a server (game port and RCON). Shared mode: Docker DNS
    by container name. Isolated mode: SpawnPoint *is* the host of its private engine,
    so it connects to the container's IP on the private network."""
    if not config.ISOLATED:
        return cname(sid)
    c = get_container(sid)
    if not c:
        raise OSError(f"no container for {sid}")
    cached = _addr_cache.get(sid)
    if cached and cached[0] == c.id:
        return cached[1]
    c.reload()
    nets = c.attrs.get("NetworkSettings", {}).get("Networks", {})
    ip = (nets.get(config.DOCKER_NETWORK) or next(iter(nets.values()), {})).get("IPAddress")
    if not ip:
        raise OSError(f"{sid} has no IP yet")
    _addr_cache[sid] = (c.id, ip)
    return ip


def ensure_network() -> None:
    """Create the private network for server containers (if missing) and join it,
    so the built-in router can reach mc-<name>:25565 by container name. This means
    the Unraid template can stay on the normal bridge network."""
    from docker.errors import NotFound as _NF
    try:
        net = client().networks.get(config.DOCKER_NETWORK)
    except _NF:
        net = client().networks.create(config.DOCKER_NETWORK, driver="bridge")
        log.info("created docker network %s", config.DOCKER_NETWORK)
    if config.ISOLATED:
        return  # SpawnPoint hosts the private engine; it reaches containers by IP
    try:
        me = client().containers.get(socket.gethostname())
    except Exception as e:
        log.warning("can't find own container to join %s: %s", config.DOCKER_NETWORK, e)
        return
    if config.DOCKER_NETWORK not in me.attrs.get("NetworkSettings", {}).get("Networks", {}):
        net.connect(me)
        log.info("joined docker network %s", config.DOCKER_NETWORK)


def trusted_sources() -> list[str]:
    """Addresses SpawnPoint's router connects to game servers from: the private
    network's subnet (covers SpawnPoint in both modes), its gateway, and SpawnPoint's
    own address on it in shared mode."""
    out: list[str] = []
    try:
        net = client().networks.get(config.DOCKER_NETWORK)
        for cfg in (net.attrs.get("IPAM", {}).get("Config") or []):
            if cfg.get("Subnet"):
                out.append(cfg["Subnet"])
            if cfg.get("Gateway"):
                out.append(cfg["Gateway"])
        if not config.ISOLATED:
            me = client().containers.get(socket.gethostname())
            ip = me.attrs["NetworkSettings"]["Networks"].get(config.DOCKER_NETWORK, {}).get("IPAddress")
            if ip:
                out.append(ip)
    except Exception as e:
        log.warning("couldn't read %s addresses: %s", config.DOCKER_NETWORK, e)
    return out or ["172.16.0.0/12"]


def host_path(path: str) -> str:
    """Translate a path inside this container to the host path, for sibling bind mounts."""
    if config.ISOLATED:
        return path  # the private engine runs inside SpawnPoint and sees the same paths
    if config.DATA_HOST_DIR and (path == config.DATA_DIR or path.startswith(config.DATA_DIR + "/")):
        return config.DATA_HOST_DIR.rstrip("/") + path[len(config.DATA_DIR):]
    for dest, src in _own_mounts():
        if path == dest or path.startswith(dest + "/"):
            return src + path[len(dest):]
    return path


def get_container(sid: str):
    try:
        return client().containers.get(cname(sid))
    except NotFound:
        return None


def status(sid: str) -> str:
    c = get_container(sid)
    return c.status if c else "absent"


def exit_info(sid: str) -> dict | None:
    """How a stopped container ended: exit code, whether the kernel killed it for memory,
    and when it started and finished."""
    c = get_container(sid)
    if not c or c.status == "running":
        return None
    st = c.attrs.get("State", {})
    return {"code": st.get("ExitCode"), "oom": bool(st.get("OOMKilled")),
            "started": st.get("StartedAt"), "finished": st.get("FinishedAt")}


def started_at(sid: str) -> str | None:
    c = get_container(sid)
    return c.attrs.get("State", {}).get("StartedAt") if c and c.status == "running" else None


def normal_gb(s: dict) -> int:
    """A server's normal (soft) heap size. Older records only had max_gb."""
    m = s["memory"]
    return min(m.get("soft_gb") or m["max_gb"], m["max_gb"])


def build_env(s: dict, worker_threads: int) -> dict:
    tag = s["java_tag"]
    env = {
        "EULA": "TRUE",
        "TZ": config.TZ,
        "ENABLE_RCON": "true",
        "RCON_PASSWORD": s["rcon_password"],
        "INIT_MEMORY": f"{s['memory']['init_gb']}G",
        "MAX_MEMORY": f"{s['memory']['max_gb']}G",
        "JVM_OPTS": perf.jvm_opts(tag, worker_threads, normal_gb(s)),
        "USE_AIKAR_FLAGS": "true" if perf.use_aikar(tag) else "false",
        "STOP_SERVER_ANNOUNCE_DELAY": "5",
        "ENABLE_AUTOPAUSE": "false",
        "SKIP_SERVER_PROPERTIES": "true",  # we own server.properties after first boot
    }
    kind = s["kind"]
    if kind == "modrinth":
        env.update(TYPE="MODRINTH", MODRINTH_MODPACK=s["pack"]["project"])
        if s["pack"].get("version_id"):
            env["MODRINTH_VERSION"] = s["pack"]["version_id"]
    elif kind == "curseforge":
        env.update(TYPE="AUTO_CURSEFORGE", CF_SLUG=s["pack"]["slug"], CF_API_KEY=config.CF_API_KEY)
        if s["pack"].get("file_id"):
            env["CF_FILE_ID"] = str(s["pack"]["file_id"])   # pinned: a restart never changes the pack
    elif kind == "ftb":
        env.update(TYPE="FTBA", FTB_MODPACK_ID=str(s["pack"]["id"]))
        if s["pack"].get("version_id"):
            env["FTB_MODPACK_VERSION_ID"] = str(s["pack"]["version_id"])
    else:
        env.update(TYPE=kind.upper(), VERSION=s["mc_version"] or "LATEST")
        lv = s.get("loader_version")
        if lv:  # imported servers: install the exact loader they were built with
            key = {"forge": "FORGE_VERSION", "neoforge": "NEOFORGE_VERSION",
                   "fabric": "FABRIC_LOADER_VERSION", "quilt": "QUILT_LOADER_VERSION"}.get(kind)
            if key:
                env[key] = lv
    if s.get("first_boot"):
        # Only on the very first boot let itzg write server.properties defaults.
        env["SKIP_SERVER_PROPERTIES"] = "false"
        env["VIEW_DISTANCE"] = str(config.DEFAULT_VIEW_DISTANCE)
        env["SIMULATION_DISTANCE"] = str(config.DEFAULT_SIM_DISTANCE)
        env["MOTD"] = s["name"]
        if s.get("seed"):
            env["SEED"] = s["seed"]           # level-seed for the new world (numbers or any text, like the game)
        env["ALLOW_FLIGHT"] = "TRUE"   # modded servers kick for "flying" on jetpacks, elytra mods and lag
    mm = s.get("managed_mods") or []
    if mm:
        # Pinned versions, so the image doesn't have to work out the pack's version itself.
        env["MODRINTH_PROJECTS"] = ",".join(f"{m['slug']}:{m['version_id']}" for m in mm)
        if s.get("loader") in ("forge", "neoforge", "fabric", "quilt"):
            env["MODRINTH_LOADER"] = s["loader"]
        env["MODRINTH_DOWNLOAD_DEPENDENCIES"] = "required"
    env.update(s.get("extra_env", {}))
    return env


def replace_container(s: dict, cpus: list[int], worker_threads: int, start: bool = True):
    """(Re)create the server container from state. World data lives in the bind mount,
    so recreating is how config changes (RAM, Java, flags) are applied."""
    sid = s["id"]
    old = get_container(sid)
    if old:
        if old.status == "running":
            old.stop(timeout=120)
        old.remove()
    os.makedirs(server_dir(sid), exist_ok=True)
    image = f"{config.IMAGE}:{s['java_tag']}"
    try:
        client().images.get(image)
    except NotFound:
        log.info("pulling %s", image)
        client().images.pull(config.IMAGE, tag=s["java_tag"])
    mem_limit = f"{perf.container_limit_gb(s['memory']['max_gb'], s.get('mod_count'))}g"
    c = client().containers.create(
        image,
        name=cname(sid),
        environment=build_env(s, worker_threads),
        volumes={host_path(server_dir(sid)): {"bind": "/data", "mode": "rw"}},
        network=config.DOCKER_NETWORK,
        cpuset_cpus=topology.format_cpu_list(cpus) if cpus else None,
        mem_limit=mem_limit,
        labels={"spawnpoint.server": sid},
        restart_policy={"Name": "no"},
        stdin_open=True, tty=False,
        stop_signal="SIGTERM",
    )
    if start:
        c.start()
    return c


def engine_info() -> dict:
    try:
        i = client().info()
        return {"isolated": config.ISOLATED, "storage_driver": i.get("Driver"),
                "containers": i.get("Containers"), "images": i.get("Images")}
    except Exception as e:
        return {"isolated": config.ISOLATED, "error": str(e)}


def set_cpuset(sid: str, cpus: list[int]) -> None:
    c = get_container(sid)
    if c and cpus:
        c.update(cpuset_cpus=topology.format_cpu_list(cpus))


# ---------------------------------------------------------------- logs
class LogHub:
    """Follows a container's logs in a thread; keeps a ring buffer and a line counter
    so callers can wait for patterns that appear *after* a given point."""

    def __init__(self, sid: str):
        self.sid = sid
        self.lines: collections.deque[tuple[int, str]] = collections.deque(maxlen=2000)
        self.seq = 0
        self.cond = threading.Condition()
        self.thread: threading.Thread | None = None

    def ensure_running(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        c = get_container(self.sid)
        if not c:
            return
        try:
            since = int(time.time()) - 2
            buf = b""
            for chunk in c.logs(stream=True, follow=True, since=since):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._push(line.decode("utf-8", "replace").rstrip("\r"))
        except Exception as e:
            log.debug("log stream ended for %s: %s", self.sid, e)

    def _push(self, line: str):
        with self.cond:
            self.seq += 1
            self.lines.append((self.seq, line))
            self.cond.notify_all()

    def since(self, seq: int) -> list[tuple[int, str]]:
        return [(n, l) for n, l in list(self.lines) if n > seq]

    def wait_for(self, needles: list[str], after: int, timeout: float) -> str | None:
        deadline = time.time() + timeout
        with self.cond:
            while True:
                for n, l in self.lines:
                    if n > after and any(x in l for x in needles):
                        return l
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.cond.wait(min(left, 5))
                if not (self.thread and self.thread.is_alive()):
                    self.ensure_running()


_hubs: dict[str, LogHub] = {}


def hub(sid: str) -> LogHub:
    if sid not in _hubs:
        _hubs[sid] = LogHub(sid)
    return _hubs[sid]


def reset_hub(sid: str) -> LogHub:
    """Fresh buffer for a freshly (re)created container, so old 'Done' lines don't match."""
    _hubs[sid] = LogHub(sid)
    return _hubs[sid]


def recent_logs(sid: str, tail: int = 300) -> list[str]:
    c = get_container(sid)
    if not c:
        return []
    return c.logs(tail=tail).decode("utf-8", "replace").splitlines()


# ---------------------------------------------------------------- stats
def mem_usage_bytes(sid: str) -> int:
    c = get_container(sid)
    if not c or c.status != "running":
        return 0
    try:
        st = c.stats(stream=False)
        m = st.get("memory_stats", {})
        cache = m.get("stats", {}).get("inactive_file", 0)
        return max(0, m.get("usage", 0) - cache)
    except Exception:
        return 0


def cpu_percent(sid: str) -> float:
    c = get_container(sid)
    if not c or c.status != "running":
        return 0.0
    try:
        st = c.stats(stream=False)
        cd = st["cpu_stats"]["cpu_usage"]["total_usage"] - st["precpu_stats"]["cpu_usage"]["total_usage"]
        sd = st["cpu_stats"].get("system_cpu_usage", 0) - st["precpu_stats"].get("system_cpu_usage", 0)
        n = st["cpu_stats"].get("online_cpus", 1)
        return round(cd / sd * n * 100, 1) if sd > 0 else 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------- thread pinning
def _children(pid: int) -> list[int]:
    out = []
    for t in os.listdir(f"/proc/{pid}/task"):
        try:
            with open(f"/proc/{pid}/task/{t}/children") as f:
                out += [int(x) for x in f.read().split()]
        except OSError:
            pass
    return out


def _find_java(pid: int, depth: int = 0) -> int | None:
    try:
        with open(f"/proc/{pid}/comm") as f:
            if f.read().strip() == "java":
                return pid
    except OSError:
        return None
    if depth > 4:
        return None
    for ch in _children(pid):
        j = _find_java(ch, depth + 1)
        if j:
            return j
    return None


def find_java_by_cgroup(container_id: str, proc: str = "/proc") -> int | None:
    """Find the server's java process by its cgroup, for kernels without
    /proc/<pid>/task/<tid>/children (where walking child processes finds nothing)."""
    for d in os.listdir(proc):
        if not d.isdigit():
            continue
        try:
            with open(f"{proc}/{d}/comm") as f:
                if f.read().strip() != "java":
                    continue
            with open(f"{proc}/{d}/cgroup") as f:
                if container_id in f.read():
                    return int(d)
        except OSError:
            continue
    return None


def pin_threads(sid: str, main_cpus: list[int]) -> dict:
    """Pin the Minecraft 'Server thread' to the server's own cores and lower the
    priority of world-gen workers so they never slow the game tick.
    Needs the panel container to run with pid=host and CAP_SYS_NICE; silently
    does nothing otherwise."""
    result = {"pinned": False, "workers": 0}
    if not config.PIN_SERVER_THREAD:
        return result
    c = get_container(sid)
    if not c:
        return result
    c.reload()
    root = c.attrs.get("State", {}).get("Pid")
    if not root or not os.path.exists(f"/proc/{root}"):
        return result
    jpid = _find_java(root) or find_java_by_cgroup(c.id)
    if not jpid:
        return result
    for tid in os.listdir(f"/proc/{jpid}/task"):
        try:
            with open(f"/proc/{jpid}/task/{tid}/comm") as f:
                name = f.read().strip()
            t = int(tid)
            if name == "Server thread" and main_cpus:
                os.sched_setaffinity(t, set(main_cpus))
                result["pinned"] = True
            elif name.startswith(("Worker-Main", "C2ME", "Paper Chunk", "Tuinity Chunk")):
                os.setpriority(os.PRIO_PROCESS, t, 10)
                result["workers"] += 1
        except (OSError, ValueError):
            continue
    return result
