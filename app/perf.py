"""Performance policy: Java image, heap sizing, JVM flags, core-group sizing."""
import re

MODDED_KINDS = {"fabric", "forge", "neoforge", "quilt", "modrinth", "curseforge", "ftb"}
MODPACK_KINDS = {"modrinth", "curseforge", "ftb"}


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def java_tag(mc_version: str) -> str:
    """Pick the itzg image tag for a Minecraft version.

    26.x+ (the 2026 year-based versions) need Java 25, 1.20.5+ needs Java 21,
    1.17-1.20.4 runs on 17, older packs (1.16.5 Forge etc.) want Java 8.
    """
    v = parse_version(mc_version)
    if not mc_version or mc_version.upper() == "LATEST":
        return "latest"
    if v[0] >= 26:
        return "java25"
    if v >= (1, 20, 5):
        return "java21"
    if v >= (1, 17):
        return "java17"
    return "java8"


def java_major(tag: str) -> int:
    m = re.search(r"java(\d+)", tag)
    return int(m.group(1)) if m else 25


def memory_plan(kind: str, mod_count: int | None = None) -> tuple[int, int, int]:
    """Return (start_gb, normal_gb, ceiling_gb).

    The heap starts at start_gb and grows on demand. ZGC aims to stay around
    normal_gb (its "soft max heap") and gives memory back to Unraid when quiet, but
    grows past it, up to ceiling_gb, whenever the server needs more to avoid stalling.
    Nothing is reserved: the ceiling is only reached under real pressure."""
    if kind in ("vanilla", "paper", "purpur"):
        start, normal = 2, 6
    elif kind not in MODPACK_KINDS and (mod_count is None or mod_count < 60):
        start, normal = 2, 8
    elif mod_count is None:
        start, normal = 3, 12  # modpack, not installed yet: assume heavy-ish
    elif mod_count < 60:
        start, normal = 2, 8
    elif mod_count < 150:
        start, normal = 3, 10
    elif mod_count < 300:
        start, normal = 4, 12
    else:
        start, normal = 4, 16
    return fit_to_host(start, normal, ceiling_for(normal))


def fit_to_host(start: int, normal: int, ceiling: int) -> tuple[int, int, int]:
    """On smaller machines, keep one server's heap inside the RAM pool (minus room for
    Java's own overhead), so it can't push the machine into swapping or the OOM killer."""
    from . import config
    pool = config.ram_pool_gb()
    cap = max(2, int(pool * 0.75))
    ceiling = min(ceiling, cap)
    normal = min(normal, ceiling)
    return min(start, normal), normal, ceiling


def ceiling_for(normal_gb: int) -> int:
    """How far a server may grow past its normal size when it needs to: double, at
    least +4 GB, at most +16 GB."""
    return normal_gb + min(16, max(4, normal_gb))


def home_groups(kind: str, mod_count: int | None = None) -> int:
    """How many L3 groups a server owns. Plain servers: 1 (2 cores/4 threads on a 7F72).
    Modpacks: 2, so GC and worker threads don't fight the main thread."""
    if kind in MODPACK_KINDS or (mod_count or 0) >= 60:
        return 2
    return 1


def jvm_opts(tag: str, worker_threads: int, normal_gb: int | None = None) -> str:
    """JVM flags tuned for 'grow when busy, give memory back when quiet'."""
    major = java_major(tag)
    opts: list[str] = []
    if major >= 21:
        opts += ["-XX:+UseZGC"]
        if major < 23:
            opts += ["-XX:+ZGenerational"]  # default (and flag obsolete) from 23 on
        if normal_gb:
            # Soft target: ZGC stays around it, but may grow up to -Xmx when needed.
            opts += [f"-XX:SoftMaxHeapSize={normal_gb}g"]
        opts += ["-XX:ZUncommitDelay=60", "-XX:+UseTransparentHugePages"]
    elif major >= 12:
        # G1: periodic collections and low free-ratio targets so idle heap goes back
        # to the OS promptly; it still expands freely up to -Xmx under load.
        opts += ["-XX:G1PeriodicGCInterval=15000", "-XX:MinHeapFreeRatio=10", "-XX:MaxHeapFreeRatio=30",
                 "-XX:+UseTransparentHugePages"]
    # Vanilla/Forge world-gen worker pool size (Mojang's Util background executor).
    opts += [f"-Dmax.bg.threads={max(2, worker_threads)}"]
    return " ".join(opts)


def use_aikar(tag: str) -> bool:
    return java_major(tag) < 21


def container_limit_gb(max_heap_gb: int, mod_count: int | None = None) -> int:
    """Hard memory limit for a server's container: the heap ceiling plus room for
    everything Java keeps outside the heap (class metadata, thread stacks, GC and
    native buffers). Big modpacks need a lot more of that room than vanilla."""
    extra = max(3, round(max_heap_gb * 0.35))
    if (mod_count or 0) >= 150:
        extra = max(extra, 5)
    from . import config
    limit = max_heap_gb + extra
    host = config.host_ram_gb()
    return int(min(limit, host - 2)) if host and host - 2 > max_heap_gb else limit
