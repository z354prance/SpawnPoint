"""Runtime configuration, all from environment variables."""
import os
import secrets


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

# Paths INSIDE the SpawnPoint container. Map them to your NVMe pool in the Unraid template;
# SpawnPoint reads its own mounts to find the matching host path for server containers.
DATA_DIR = os.environ.get("DATA_DIR", "/servers")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
# Optional override if auto-detection can't see the mounts (e.g. a custom container hostname).
DATA_HOST_DIR = os.environ.get("DATA_HOST_DIR", "")
STATE_DIR = os.environ.get("STATE_DIR", os.path.join(DATA_DIR, ".spawnpoint"))


def _load_secret_key() -> str:
    """Key that signs login sessions. Created on first start and kept in appdata, so
    there's nothing to set up and you stay logged in across restarts and updates."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(STATE_DIR, "secret.key")
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
    except OSError:
        pass  # read-only/unavailable: fall back to a per-start key
    return key


SECRET_KEY = _load_secret_key()

BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "mc.example.com")
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "mcnet")
# Run game servers in SpawnPoint's own private Docker engine so they don't appear in
# Unraid's Docker tab. Needs the container to be privileged. false = use the host's Docker.
ISOLATED = _bool("ISOLATED_DOCKER", True)
MC_PORT = _int("MC_PORT", 25565)  # built-in Minecraft router listens here
BEDROCK_PORTS = os.environ.get("BEDROCK_PORTS", "19132-19141")  # UDP, one per server with Bedrock players turned on
AUTO_WAKE = _bool("AUTO_WAKE", True)  # joining a stopped/sleeping server starts it
DEFAULT_SLEEP_MIN = _int("DEFAULT_SLEEP_MIN", 30)  # new servers sleep after this many empty minutes (0 = never)
DISK_LOW_GB = _int("DISK_LOW_GB", 20)            # warn (banner + alert) below this much free space
DISK_CRITICAL_GB = _int("DISK_CRITICAL_GB", 5)   # pause backups, pre-generation and maps below this
LAG_REPORT_MS = _int("LAG_REPORT_MS", 50)        # record a spark report when the slowest ticks stay above this
IMAGE = os.environ.get("MC_IMAGE", "itzg/minecraft-server")
TZ = os.environ.get("TZ", "UTC")

CF_API_KEY = os.environ.get("CF_API_KEY", "")

# Who may tell SpawnPoint a player's real IP with a Proxy Protocol header: your reverse
# proxy or tunnel. Players connecting straight in can't, so nobody can fake their address.
TRUSTED_PROXIES = os.environ.get(
    "TRUSTED_PROXIES", "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,::1/128,fc00::/7")

# Resources
_RAM_POOL_SET = os.environ.get("RAM_POOL_GB", "").strip()
RAM_POOL_GB = _int("RAM_POOL_GB", 96)    # used as-is when set; otherwise see ram_pool_gb()
_host_gb: float | None = None


def host_ram_gb() -> float:
    """Total RAM of the machine (the container sees the host's /proc/meminfo)."""
    global _host_gb
    if _host_gb is None:
        try:
            with open("/proc/meminfo") as f:
                kb = next(int(l.split()[1]) for l in f if l.startswith("MemTotal:"))
            _host_gb = kb / 2**20
        except (OSError, StopIteration, ValueError):
            _host_gb = 0.0
    return _host_gb


def ram_pool_gb() -> int:
    """How much RAM Minecraft servers may use in total. RAM_POOL_GB if set, otherwise
    three quarters of the machine's RAM (leaving at least 4 GB for everything else),
    at most 96 GB: 96 on a 256 GB box, 24 on 32 GB, 12 on 16 GB."""
    if _RAM_POOL_SET:
        return RAM_POOL_GB
    total = host_ram_gb()
    if not total:
        return RAM_POOL_GB
    return int(max(4, min(96, total * 0.75, total - 4)))
RESERVED_CORE_GROUPS = _int("RESERVED_CORE_GROUPS", 2)  # L3 groups left for Unraid & other apps
BURST_GROUPS = _int("BURST_GROUPS", 3)  # groups lent to a server while it pre-generates
MAX_BURST_SERVERS = _int("MAX_BURST_SERVERS", 2)
PIN_SERVER_THREAD = _bool("PIN_SERVER_THREAD", True)  # needs pid=host + CAP_SYS_NICE
SYSFS_CPU = os.environ.get("SYSFS_CPU", "/sys/devices/system/cpu")

# Pre-generation
SPAWN_PREGEN_RADIUS = _int("SPAWN_PREGEN_RADIUS", 750)
FULL_PREGEN_RADIUS = _int("FULL_PREGEN_RADIUS", 2500)
# Where the companion app's releases are, for the download links on player pack pages
LAUNCHER_REPO = os.environ.get("LAUNCHER_REPO", "z354prance/SpawnPoint-Launcher")
MSPT_PAUSE = _int("MSPT_PAUSE", 40)
MSPT_RESUME = _int("MSPT_RESUME", 30)

# Defaults applied to new servers
DEFAULT_VIEW_DISTANCE = _int("DEFAULT_VIEW_DISTANCE", 10)
DEFAULT_SIM_DISTANCE = _int("DEFAULT_SIM_DISTANCE", 8)

INSTALL_TIMEOUT_S = _int("INSTALL_TIMEOUT_S", 45 * 60)
