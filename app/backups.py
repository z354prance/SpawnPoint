"""Incremental backups with restic.

All servers share one restic repository in BACKUP_DIR/restic, each server's backups
tagged with its id as the "host". A backup only stores the parts of files that changed
since the last one (restic splits files into content-defined chunks, so a region file
where a few chunks changed costs a few chunks, not the whole file), compressed, and
anything already stored for another server (the same modpack's mods and libraries) is
stored once. Every backup is still a complete restore point.

Older backups stay listed and restorable: per-server repositories (BACKUP_DIR/<server>/
restic, from the first version with restic) and .tar.gz archives from before that.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import time
from datetime import datetime

from . import config, dockerctl, rcon, state

log = logging.getLogger("spawnpoint.backups")

RESTIC = os.environ.get("RESTIC_BIN", "restic")
SNAP_ID = re.compile(r"^[0-9a-f]{8,64}$")
DEFAULT_KEEP = {"last": 6, "hourly": 24, "daily": 7, "weekly": 4, "monthly": 3}
PRUNE_EVERY_S = 24 * 3600
_locks: dict[str, asyncio.Lock] = {}


class BackupError(Exception):
    pass


def server_backup_dir(sid: str) -> str:
    return os.path.join(config.BACKUP_DIR, sid)


def shared_repo() -> str:
    return os.path.join(config.BACKUP_DIR, "restic")


def old_repo(sid: str) -> str:
    """Where a server's backups went before the shared store."""
    return os.path.join(server_backup_dir(sid), "restic")


def _exists(repo: str) -> bool:
    return os.path.exists(os.path.join(repo, "config"))


_shared_lock = asyncio.Lock()      # forget/prune need the shared store to themselves


def _password_file() -> str:
    """One random key for all repositories, kept next to them so the backups stay
    readable even if SpawnPoint's own state folder is lost."""
    p = os.path.join(config.BACKUP_DIR, ".restic-password")
    if not os.path.exists(p):
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.write(secrets.token_urlsafe(32))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    return p


def _env(repo: str) -> dict:
    return {**os.environ, "RESTIC_REPOSITORY": repo, "RESTIC_PASSWORD_FILE": _password_file(),
            "RESTIC_CACHE_DIR": os.path.join(config.BACKUP_DIR, ".restic-cache"), "RESTIC_PROGRESS_FPS": "0.5"}


def _lowprio() -> list[str]:
    """Run backups below the game servers so they never cost a tick."""
    pre = []
    if shutil.which("nice"):
        pre += ["nice", "-n", "15"]
    if shutil.which("ionice"):
        pre += ["ionice", "-c", "3"]
    return pre


async def _run(repo: str, *args: str, on_line=None, low: bool = False) -> str:
    # several servers can back up into the shared store at once; when one is tidying up
    # (which needs the store to itself) the others wait for it instead of failing
    if args and args[0] not in ("init", "version"):
        args = (args[0], "--retry-lock", "5m", *args[1:])
    proc = await asyncio.create_subprocess_exec(
        *(_lowprio() if low else []), RESTIC, *args, env=_env(repo),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out: list[str] = []
    assert proc.stdout
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip("\n")
        out.append(line)
        if on_line:
            try:
                on_line(line)
            except Exception:
                pass
    err = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
    code = await proc.wait()
    if code not in (0, 3):                 # 3 = finished, but some files couldn't be read (they changed mid-backup)
        raise BackupError((err.strip().splitlines() or [f"restic exited with {code}"])[-1][:300])
    return "\n".join(out)


async def ensure_repo(repo: str | None = None) -> str:
    repo = repo or shared_repo()
    if not _exists(repo):
        os.makedirs(repo, exist_ok=True)
        await _run(repo, "init", "--repository-version", "2")
    return repo


def _set(sid: str, **fields) -> None:
    s = state.get(sid)
    if s is not None:
        cur = dict(s.get("backup") or {})
        cur.update(fields)
        state.update(sid, backup=cur)


async def create(sid: str, reason: str = "manual") -> dict:
    """Back up a server. Safe while it runs: saving is paused and flushed first."""
    from . import servers
    lock = _locks.setdefault(sid, asyncio.Lock())
    if lock.locked():
        raise BackupError("A backup of this server is already running")
    async with lock:
        from . import hostinfo
        s = state.get(sid)
        if hostinfo.disk_level() == "critical":
            g = hostinfo.disk_guard()
            raise BackupError(f"Not enough disk space ({g['free_gb']} GB free on the {g['where']} disk), so backups are paused")
        repo = await ensure_repo()
        running = dockerctl.status(sid) == "running"
        if running:
            try:
                await servers.arcmd(s, "save-off")
                await servers.arcmd(s, "save-all flush")
            except rcon.RconError:
                running = False
        _set(sid, status="running", percent=0, started=time.time(), reason=reason)
        summary: dict = {}

        def progress(line: str) -> None:
            if not line.startswith("{"):
                return
            m = json.loads(line)
            if m.get("message_type") == "status" and m.get("percent_done") is not None:
                _set(sid, percent=round(m["percent_done"] * 100, 1))
            elif m.get("message_type") == "summary":
                summary.update(m)
        try:
            await _run(repo, "backup", "--json", "--host", sid, "--tag", "spawnpoint", "--tag", reason,
                       "--exclude", "*.lock", "--exclude", "cache", "--exclude", ".cache",
                       dockerctl.server_dir(sid), on_line=progress, low=True)
        except BackupError as e:
            from . import alerts
            _set(sid, status="error", note=str(e), finished=time.time())
            alerts.send("backup_failed", s.get("name", sid), f"Backup of {s.get('name', sid)} failed", str(e), "bad")
            raise
        finally:
            if running:
                try:
                    await servers.arcmd(s, "save-on")
                except rcon.RconError:
                    pass
        _set(sid, status="done", percent=100, finished=time.time(), note="",
             added=summary.get("data_added_packed") or summary.get("data_added"))
        await apply_retention(sid)
        return {"id": summary.get("snapshot_id"), "added": summary.get("data_added_packed")}


def keep_rules(s: dict) -> dict:
    """How many backups to keep, per kind. Once set, the server's own rules replace the defaults."""
    own = s.get("backup_keep")
    return {k: int((own or DEFAULT_KEEP).get(k) or 0) for k in DEFAULT_KEEP}


def _prune_marker() -> str:
    return os.path.join(config.BACKUP_DIR, ".last-prune")


async def apply_retention(sid: str) -> None:
    """Forget this server's backups outside its keep rules; free their space once a day."""
    s = state.get(sid)
    k = keep_rules(s)
    keep = []
    for name in ("last", "hourly", "daily", "weekly", "monthly"):
        if int(k.get(name) or 0) > 0:
            keep += [f"--keep-{name}", str(int(k[name]))]
    if not keep:                                         # nothing to keep by: keep everything
        return
    try:
        last = os.path.getmtime(_prune_marker())
    except OSError:
        last = 0
    prune = time.time() - last > PRUNE_EVERY_S
    repos = [r for r in [shared_repo()] + ([old_repo(sid)] if _exists(old_repo(sid)) else []) if _exists(r)]
    async with _shared_lock:
        for repo in repos:
            try:
                await _run(repo, "forget", "--host", sid, "--tag", "spawnpoint", "--group-by", "host", *keep, low=True)
            except BackupError as e:
                log.warning("[%s] retention: %s", sid, e)
                return
    # Freeing the space goes through every server's backups and can take many minutes on
    # a big store: it runs on its own, so the backup (and an upgrade waiting on it) is done.
    global _prune_task
    if prune and (_prune_task is None or _prune_task.done()):
        _prune_task = asyncio.create_task(_prune(repos))


_prune_task: asyncio.Task | None = None


async def _prune(repos: list[str]) -> None:
    async with _shared_lock:
        for repo in repos:
            try:
                await _run(repo, "prune", low=True)
            except BackupError as e:
                log.warning("freeing backup space: %s", e)
                return
        with open(_prune_marker(), "w"):
            pass


async def snapshots(sid: str) -> list[dict]:
    out = []
    for repo in (shared_repo(), old_repo(sid)):
        if not _exists(repo):
            continue
        raw = await _run(repo, "snapshots", "--json", "--host", sid)
        for snap in json.loads(raw or "[]"):
            summ = snap.get("summary") or {}
            tags = [x for x in snap.get("tags") or [] if x != "spawnpoint"]
            out.append({"name": snap["id"], "short": snap.get("short_id", snap["id"][:8]), "time": _parse_time(snap.get("time", "")),
                        "kind": "snapshot", "size": summ.get("total_bytes_processed"),
                        "added": summ.get("data_added_packed", summ.get("data_added")), "reason": tags[0] if tags else ""})
    return out


def _parse_time(t: str) -> float:
    """restic's RFC 3339 times carry nanoseconds, which datetime doesn't take."""
    try:
        return datetime.fromisoformat(re.sub(r"\.\d+", "", t).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def legacy(sid: str) -> list[dict]:
    d = server_backup_dir(sid)
    if not os.path.isdir(d):
        return []
    return [{"name": f, "size": os.path.getsize(os.path.join(d, f)), "time": os.path.getmtime(os.path.join(d, f)),
             "kind": "archive"} for f in os.listdir(d) if f.endswith(".tar.gz")]


async def list_all(sid: str) -> list[dict]:
    try:
        snaps = await snapshots(sid)
    except BackupError as e:
        log.warning("[%s] listing backups: %s", sid, e)
        snaps = []
    return sorted(snaps + legacy(sid), key=lambda b: b["time"], reverse=True)


async def _locate(sid: str, snap: str) -> tuple[str, str]:
    """(repository, path inside the backup) for one of this server's backups."""
    for repo in (shared_repo(), old_repo(sid)):
        if not _exists(repo):
            continue
        found = json.loads(await _run(repo, "snapshots", "--json", "--host", sid, snap) or "[]")
        if found:
            return repo, found[0]["paths"][0]
    raise BackupError("That backup doesn't exist")


async def restore(sid: str, name: str) -> None:
    """Replace the server's files with a backup. The current files are kept next to it
    in a .pre-restore folder."""
    if dockerctl.status(sid) == "running":
        raise ValueError("Stop the server before restoring.")
    sdir = dockerctl.server_dir(sid)
    old = sdir + ".pre-restore"
    if name.endswith(".tar.gz"):
        src = os.path.join(server_backup_dir(sid), os.path.basename(name))

        def _untar():
            shutil.rmtree(old, ignore_errors=True)
            os.rename(sdir, old)
            with tarfile.open(src) as t:
                t.extractall(config.DATA_DIR, filter="data")
        await asyncio.to_thread(_untar)
        return
    if not SNAP_ID.match(name):
        raise ValueError("Unknown backup")
    repo, path = await _locate(sid, name)
    tmp = sdir + ".restoring"
    await asyncio.to_thread(shutil.rmtree, tmp, True)
    await _run(repo, "restore", f"{name}:{path}", "--target", tmp)

    def _swap():
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(sdir):
            os.rename(sdir, old)
        os.rename(tmp, sdir)
    await asyncio.to_thread(_swap)


def _clean_rel(path: str) -> str:
    """A path inside a backup, like /world/playerdata. Refuses anything that climbs out."""
    parts = [x for x in (path or "/").replace("\\", "/").split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise ValueError("Bad path")
    return "/" + "/".join(parts)


async def browse(sid: str, name: str, path: str = "/") -> list[dict]:
    """What's directly inside one folder of a backup."""
    if not SNAP_ID.match(name):
        raise ValueError("Only incremental backups can be browsed")
    rel = _clean_rel(path)
    repo, root = await _locate(sid, name)
    raw = await _run(repo, "ls", "--json", f"{name}:{root}", rel)
    out = []
    for line in raw.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        p = e.get("path")
        if not p or e.get("struct_type") == "snapshot" or p == rel:
            continue
        parent = p.rsplit("/", 1)[0] or "/"
        if parent != rel:
            continue                                     # only the folder's own entries
        out.append({"name": e.get("name"), "path": p, "dir": e.get("type") == "dir", "size": e.get("size")})
    return sorted(out, key=lambda x: (not x["dir"], x["name"].lower()))


async def restore_paths(sid: str, name: str, paths: list[str]) -> dict:
    """Put chosen files or folders back from a backup, leaving everything else alone. What
    they replace is kept in <server>.pre-restore-files/<time>/."""
    if dockerctl.status(sid) == "running":
        raise ValueError("Stop the server before restoring: it would overwrite the files again.")
    if not SNAP_ID.match(name):
        raise ValueError("Only incremental backups can be restored in parts")
    rels = sorted({_clean_rel(p) for p in paths if p and _clean_rel(p) != "/"})
    if not rels:
        raise ValueError("Pick something to restore")
    repo, root = await _locate(sid, name)
    sdir = dockerctl.server_dir(sid)
    tmp = sdir + ".restoring-files"
    await asyncio.to_thread(shutil.rmtree, tmp, True)
    args = ["restore", f"{name}:{root}", "--target", tmp]
    for r in rels:
        args += ["--include", r]
    await _run(repo, *args)
    keep = os.path.join(sdir + ".pre-restore-files", time.strftime("%Y%m%d-%H%M%S"))

    def _place() -> int:
        n = 0
        for r in rels:
            src = os.path.join(tmp, r.lstrip("/"))
            if not os.path.exists(src):
                continue
            dst = os.path.join(sdir, r.lstrip("/"))
            if os.path.lexists(dst):
                aside = os.path.join(keep, r.lstrip("/"))
                os.makedirs(os.path.dirname(aside), exist_ok=True)
                shutil.move(dst, aside)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            n += 1
        shutil.rmtree(tmp, ignore_errors=True)
        return n
    n = await asyncio.to_thread(_place)
    if not n:
        raise ValueError("Those files aren't in this backup")
    return {"restored": n, "kept": os.path.relpath(keep, os.path.dirname(sdir)) if os.path.exists(keep) else None}


async def export_zip(sid: str, name: str):
    """Stream one backup as a zip, made on the fly (nothing extra is written to disk)."""
    if not SNAP_ID.match(name):
        raise ValueError("Unknown backup")
    repo, path = await _locate(sid, name)
    proc = await asyncio.create_subprocess_exec(
        RESTIC, "dump", "--archive", "zip", f"{name}:{path}", "/", env=_env(repo),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def gen():
        assert proc.stdout
        try:
            while chunk := await proc.stdout.read(1 << 20):
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
    return gen()


async def delete(sid: str, name: str) -> None:
    if name.endswith(".tar.gz"):
        p = os.path.join(server_backup_dir(sid), os.path.basename(name))
        if os.path.exists(p):
            os.remove(p)
        return
    if not SNAP_ID.match(name):
        raise ValueError("Unknown backup")
    repo, _ = await _locate(sid, name)
    async with _shared_lock:
        await _run(repo, "forget", "--prune", name, low=True)


async def store_size() -> int:
    """Bytes used by all backups of all servers."""
    def walk() -> int:
        total = 0
        for root, _, files in os.walk(config.BACKUP_DIR):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    return await asyncio.to_thread(walk)
