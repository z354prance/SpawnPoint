"""CPU topology and core-group allocation.

On Zen 2 EPYC (e.g. 7F72) each L3 slice is shared by a small group of cores
(2 cores / 4 threads on the 7F72). We treat each L3 group as an allocation unit so
a server's main thread never shares cache with another server.
"""
import glob
import os
import threading

from . import config


def parse_cpu_list(s: str) -> list[int]:
    out: list[int] = []
    for part in s.strip().split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def format_cpu_list(cpus: list[int]) -> str:
    return ",".join(str(c) for c in sorted(set(cpus)))


def _siblings(sysfs: str, cpu: int) -> list[int]:
    try:
        with open(os.path.join(sysfs, f"cpu{cpu}", "topology", "thread_siblings_list")) as f:
            return parse_cpu_list(f.read())
    except OSError:
        return [cpu]


def split_group(sysfs: str, cpus: list[int], per_unit: int = 2) -> list[list[int]]:
    """Split one big L3 group into units of `per_unit` physical cores (with their SMT
    threads). Desktop chips share one L3 across all cores, which would otherwise make
    the whole CPU a single unit."""
    cores: list[list[int]] = []
    seen: set[int] = set()
    for c in sorted(cpus):
        if c in seen:
            continue
        sib = sorted(set(_siblings(sysfs, c)) & set(cpus)) or [c]
        seen.update(sib)
        cores.append(sib)
    return [sorted(x for core in cores[i:i + per_unit] for x in core) for i in range(0, len(cores), per_unit)]


def detect_groups(sysfs: str | None = None) -> list[list[int]]:
    """Allocation units as lists of logical CPU ids, ordered by first CPU: L3 cache
    groups (2 cores / 4 threads on a 7F72), with bigger groups split into 2-core units."""
    sysfs = sysfs or config.SYSFS_CPU
    groups: dict[tuple, list[int]] = {}
    for path in glob.glob(os.path.join(sysfs, "cpu[0-9]*", "cache", "index3", "shared_cpu_list")):
        try:
            with open(path) as f:
                cpus = parse_cpu_list(f.read())
        except OSError:
            continue
        groups[tuple(cpus)] = cpus
    if not groups:
        # Fallback: pair up SMT siblings two cores at a time.
        n = os.cpu_count() or 4
        return [list(range(i, min(i + 4, n))) for i in range(0, n, 4)]
    out: list[list[int]] = []
    for g in groups.values():
        out += split_group(sysfs, g) if len(g) > 4 else [g]
    return sorted(out, key=lambda g: g[0])


def reserved_count(n_units: int, wanted: int) -> int:
    """Units kept for the host: as configured, but never more than a third of the CPU,
    so Minecraft always gets at least two thirds (on a 4-core desktop: none reserved)."""
    return max(0, min(wanted, n_units // 3))


class CoreAllocator:
    """Hands out L3 groups: 'home' groups per running server, plus 'burst' groups
    lent temporarily during pre-generation."""

    def __init__(self, groups: list[list[int]], reserved: int):
        self.groups = groups
        self.reserved = groups[:reserved]
        self.usable = list(range(reserved, len(groups)))  # indexes into groups
        self.home: dict[str, list[int]] = {}
        self.burst: dict[str, list[int]] = {}
        self.lock = threading.Lock()

    def _in_use(self) -> dict[int, int]:
        use = {i: 0 for i in self.usable}
        for gl in list(self.home.values()) + list(self.burst.values()):
            for g in gl:
                use[g] = use.get(g, 0) + 1
        return use

    def assign_home(self, sid: str, count: int) -> list[int]:
        with self.lock:
            if sid in self.home:
                return self.home[sid]
            use = self._in_use()
            # Prefer completely free groups, then least shared.
            ranked = sorted(self.usable, key=lambda g: (use[g], g))
            chosen = ranked[: max(1, min(count, len(ranked)))]
            self.home[sid] = chosen
            return chosen

    def release(self, sid: str) -> None:
        with self.lock:
            self.home.pop(sid, None)
            self.burst.pop(sid, None)

    def lend_burst(self, sid: str, count: int, max_servers: int) -> list[int]:
        with self.lock:
            if sid in self.burst:
                return self.burst[sid]
            # map renders and pre-generation each have their own limit on borrowing servers
            same_kind = [k for k in self.burst if k.startswith("map:") == sid.startswith("map:")]
            if len(same_kind) >= max_servers:
                return []
            use = self._in_use()
            free = [g for g in self.usable if use[g] == 0]
            chosen = free[:count]
            if chosen:
                self.burst[sid] = chosen
            return chosen

    def return_burst(self, sid: str) -> None:
        with self.lock:
            self.burst.pop(sid, None)

    def cpus_for(self, sid: str, include_burst: bool = True) -> list[int]:
        idx = list(self.home.get(sid, []))
        if include_burst:
            idx += self.burst.get(sid, [])
        return sorted(c for i in idx for c in self.groups[i])

    def summary(self) -> dict:
        use = self._in_use()
        return {
            "groups": [
                {
                    "index": i,
                    "cpus": g,
                    "reserved": i < len(self.reserved),
                    "home": [s for s, gl in self.home.items() if i in gl],
                    "burst": [s for s, gl in self.burst.items() if i in gl],
                    "load": use.get(i, 0),
                }
                for i, g in enumerate(self.groups)
            ]
        }


_allocator: CoreAllocator | None = None


def allocator() -> CoreAllocator:
    global _allocator
    if _allocator is None:
        g = detect_groups()
        _allocator = CoreAllocator(g, reserved_count(len(g), config.RESERVED_CORE_GROUPS))
    return _allocator
