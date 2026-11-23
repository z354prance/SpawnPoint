"""The Files tab: a file manager for a server's folder.

Everything works on paths relative to the server's folder and is checked to stay inside
it (symlinks included). Deleting moves things to a per-server "Recently deleted" folder
kept for 7 days, so a wrong click on a world can be undone. Files SpawnPoint creates are
given to the owner of the server's folder, because the game doesn't run as SpawnPoint's
user and has to be able to change them."""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import stat
import tarfile
import time
import uuid
import zipfile

from . import config, dockerctl

log = logging.getLogger("spawnpoint.files")

MAX_EDIT = 5 * 2**20
TRASH_DAYS = 7
SEARCH_LIMIT = 500
STORED = (".jar", ".zip", ".mca", ".mcc", ".png", ".jpg", ".jpeg", ".gz", ".xz", ".zst", ".7z", ".ogg", ".mp3", ".dat")
TEXT_HINT = (".json", ".json5", ".toml", ".yml", ".yaml", ".properties", ".cfg", ".conf", ".txt", ".log", ".js", ".zs",
             ".mcmeta", ".snbt", ".ini", ".md", ".csv", ".sh", ".xml", ".lang")


class FileError(Exception):
    """A problem to show the user as it is."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# ------------------------------------------------------------------ paths and owners
def root(sid: str) -> str:
    return os.path.realpath(dockerctl.server_dir(sid))


def clean_rel(rel: str) -> str:
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise FileError("Path outside the server's folder")
    return "/".join(parts)


def resolve(sid: str, rel: str, must_exist: bool = True) -> str:
    base = root(sid)
    rel = clean_rel(rel)
    p = os.path.realpath(os.path.join(base, rel))
    if p != base and not p.startswith(base + os.sep):
        raise FileError("Path outside the server's folder")
    if must_exist and not os.path.lexists(p):
        raise FileError(f"{rel or '/'} doesn't exist (any more)", 404)
    return p


def rel_of(sid: str, p: str) -> str:
    return os.path.relpath(p, root(sid)).replace(os.sep, "/").removeprefix(".")


def own_like(path: str, ref: str) -> None:
    """Give path (and everything under it) to the owner of ref."""
    if os.geteuid() != 0:
        return
    try:
        st = os.stat(ref)
    except OSError:
        return
    todo = [path]
    if os.path.isdir(path) and not os.path.islink(path):
        todo = []
        for dirpath, _, filenames in os.walk(path):
            todo.append(dirpath)
            todo += [os.path.join(dirpath, f) for f in filenames]
    for n in todo:
        try:
            ls = os.lstat(n)
            if ls.st_uid != st.st_uid or ls.st_gid != st.st_gid:
                os.lchown(n, st.st_uid, st.st_gid)
        except OSError:
            pass


def _valid_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name or len(name) > 255:
        raise FileError("Names can't be empty or contain / or \\")
    return name


# ------------------------------------------------------------------ listing and reading
def _entry(p: str, name: str) -> dict:
    try:
        ls = os.lstat(p)
    except OSError:
        return {"name": name, "dir": False, "size": None, "mtime": None}
    link = stat.S_ISLNK(ls.st_mode)
    try:
        st = os.stat(p) if link else ls
    except OSError:
        st = ls
    d = stat.S_ISDIR(st.st_mode)
    return {"name": name, "dir": d, "size": None if d else st.st_size, "mtime": st.st_mtime, "link": link}


def listdir(sid: str, rel: str) -> dict:
    p = resolve(sid, rel)
    if not os.path.isdir(p):
        raise FileError("That's a file, not a folder")
    entries = [_entry(os.path.join(p, n), n) for n in os.listdir(p)]
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": clean_rel(rel), "entries": entries}


def _is_text(p: str) -> bool:
    with open(p, "rb") as f:
        head = f.read(8192)
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start > len(head) - 4                       # cut in the middle of a character


def read(sid: str, rel: str) -> dict:
    p = resolve(sid, rel)
    if os.path.isdir(p):
        raise FileError("That's a folder")
    size = os.path.getsize(p)
    if size > MAX_EDIT:
        raise FileError(f"Too big to open here ({size / 2**20:.0f} MB). Download it instead.", 413)
    if not _is_text(p):
        raise FileError("This isn't a text file. Download it instead.", 415)
    with open(p, encoding="utf-8", errors="replace", newline="") as f:
        return {"path": clean_rel(rel), "content": f.read(), "mtime": os.path.getmtime(p), "size": size}


def write(sid: str, rel: str, content: str, mtime: float | None = None, create: bool = False) -> dict:
    p = resolve(sid, rel, must_exist=False)
    exists = os.path.exists(p)
    if exists and os.path.isdir(p):
        raise FileError("That's a folder")
    if create and exists:
        raise FileError(f"{os.path.basename(p)} already exists", 409)
    if not create and not exists:
        raise FileError("The file was deleted or moved while you had it open", 409)
    if mtime is not None and exists and abs(os.path.getmtime(p) - mtime) > 0.001:
        raise FileError("The file changed since you opened it (the server may have written it). "
                        "Reload to see the new version, or save again to overwrite it.", 409)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".spawnpoint-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    if exists:
        st = os.stat(p)
        try:
            os.chown(tmp, st.st_uid, st.st_gid) if os.geteuid() == 0 else None
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
        except OSError:
            pass
    os.replace(tmp, p)
    if not exists:
        own_like(p, root(sid))
    return {"ok": True, "mtime": os.path.getmtime(p)}


# ------------------------------------------------------------------ making, renaming, moving
def mkdir(sid: str, rel: str) -> dict:
    parent, name = os.path.split(clean_rel(rel))
    _valid_name(name)
    p = os.path.join(resolve(sid, parent), name)
    if os.path.lexists(p):
        raise FileError(f"{name} already exists", 409)
    os.makedirs(p)
    own_like(p, root(sid))
    return {"path": rel_of(sid, p)}


def rename(sid: str, rel: str, new_name: str) -> dict:
    src = resolve(sid, rel)
    if src == root(sid):
        raise FileError("The server's folder itself can't be renamed")
    dst = os.path.join(os.path.dirname(src), _valid_name(new_name))
    if os.path.lexists(dst) and os.path.normcase(dst) != os.path.normcase(src):
        raise FileError(f"{new_name} already exists here", 409)
    os.rename(src, dst)
    return {"path": rel_of(sid, dst)}


def _copy(src: str, dst: str) -> None:
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True, copy_function=_copy_file)
    else:
        _copy_file(src, dst)


def _copy_file(src: str, dst: str) -> None:
    # A reflink is an instant copy on Btrfs, XFS and ZFS; elsewhere cp copies normally.
    if os.system(f"cp --reflink=auto --preserve=mode,timestamps -P -- {_q(src)} {_q(dst)} 2>/dev/null") != 0:
        shutil.copy2(src, dst, follow_symlinks=False)


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def move(sid: str, rels: list[str], to: str, copy: bool = False, overwrite: bool = False) -> dict:
    dest = resolve(sid, to)
    if not os.path.isdir(dest):
        raise FileError("Pick a folder to put them in")
    base = root(sid)
    done = []
    for rel in rels:
        src = resolve(sid, rel)
        if src == base:
            raise FileError("The server's folder itself can't be moved")
        if dest == src or dest.startswith(src + os.sep):
            raise FileError(f"{os.path.basename(src)} can't go inside itself")
        target = os.path.join(dest, os.path.basename(src))
        if os.path.lexists(target):
            if target == src and copy:
                stem, ext = os.path.splitext(os.path.basename(src))
                n = 2
                while os.path.lexists(target):
                    target = os.path.join(dest, f"{stem} ({'copy' if n == 2 else f'copy {n - 1}'}){ext}")
                    n += 1
            elif target == src:
                continue
            elif overwrite:
                _to_trash(sid, target)
            else:
                raise FileError(f"{os.path.basename(src)} is already in that folder", 409)
        if copy:
            _copy(src, target)
            own_like(target, base)
        else:
            shutil.move(src, target)
        done.append(rel_of(sid, target))
    return {"done": done}


# ------------------------------------------------------------------ recently deleted
def trash_dir(sid: str) -> str:
    return os.path.join(config.STATE_DIR, "trash", sid)


def _to_trash(sid: str, p: str) -> dict:
    tid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    box = os.path.join(trash_dir(sid), tid)
    os.makedirs(box)
    meta = {"id": tid, "path": rel_of(sid, p), "at": time.time(), "dir": os.path.isdir(p) and not os.path.islink(p)}
    with open(os.path.join(box, "meta.json"), "w") as f:
        json.dump(meta, f)
    shutil.move(p, os.path.join(box, "item"))                 # same disk as the servers: just a rename
    return meta


def delete(sid: str, rels: list[str]) -> dict:
    base = root(sid)
    out = []
    for rel in rels:
        p = resolve(sid, rel)
        if p == base:
            raise FileError("The server's folder itself can't be deleted")
        out.append(_to_trash(sid, p)["path"])
    purge_old(sid)
    return {"deleted": out}


def _size(p: str) -> int:
    if os.path.isfile(p) or os.path.islink(p):
        return os.lstat(p).st_size
    total = 0
    for dirpath, _, filenames in os.walk(p):
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def trash_list(sid: str) -> dict:
    purge_old(sid)
    d = trash_dir(sid)
    items = []
    if os.path.isdir(d):
        for tid in os.listdir(d):
            try:
                with open(os.path.join(d, tid, "meta.json")) as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            m["size"] = _size(os.path.join(d, tid, "item"))
            items.append(m)
    items.sort(key=lambda m: -m["at"])
    return {"items": items, "days": TRASH_DAYS}


def restore(sid: str, ids: list[str]) -> dict:
    d = trash_dir(sid)
    done = []
    for tid in ids:
        if not tid.replace("-", "").isalnum():
            raise FileError("Unknown item")
        box = os.path.join(d, tid)
        try:
            with open(os.path.join(box, "meta.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            raise FileError("That item isn't in Recently deleted any more", 404)
        target = resolve(sid, m["path"], must_exist=False)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.lexists(target):
            stem, ext = os.path.splitext(target)
            target = f"{stem} (restored){ext}"
            if os.path.lexists(target):
                raise FileError(f"{m['path']} and a restored copy already exist", 409)
        shutil.move(os.path.join(box, "item"), target)
        shutil.rmtree(box, ignore_errors=True)
        done.append(rel_of(sid, target))
    return {"restored": done}


def empty_trash(sid: str, ids: list[str] | None = None) -> dict:
    d = trash_dir(sid)
    gone = 0
    if os.path.isdir(d):
        for tid in os.listdir(d):
            if ids is None or tid in ids:
                shutil.rmtree(os.path.join(d, tid), ignore_errors=True)
                gone += 1
    return {"removed": gone}


def purge_old(sid: str | None = None) -> None:
    top = os.path.join(config.STATE_DIR, "trash")
    cutoff = time.time() - TRASH_DAYS * 86400
    for s in ([sid] if sid else (os.listdir(top) if os.path.isdir(top) else [])):
        d = os.path.join(top, s)
        if not os.path.isdir(d):
            continue
        for tid in os.listdir(d):
            try:
                with open(os.path.join(d, tid, "meta.json")) as f:
                    at = json.load(f)["at"]
            except (OSError, ValueError, KeyError):
                at = os.path.getmtime(os.path.join(d, tid))
            if at < cutoff:
                shutil.rmtree(os.path.join(d, tid), ignore_errors=True)


# ------------------------------------------------------------------ uploads
async def upload(sid: str, rel: str, chunks, overwrite: bool = False) -> dict:
    """Stream an uploaded file to disk. rel may include folders (a folder upload), which
    are made as needed. Written next to the target first, so a broken upload leaves
    nothing half-written behind."""
    import asyncio
    rel = clean_rel(rel)
    parent, name = os.path.split(rel)
    _valid_name(name)
    base = root(sid)
    pdir = resolve(sid, parent, must_exist=False)
    target = os.path.join(pdir, name)
    if os.path.realpath(pdir) != base and not os.path.realpath(pdir).startswith(base + os.sep):
        raise FileError("Path outside the server's folder")
    if os.path.isdir(target):
        raise FileError(f"{rel} is a folder", 409)
    if os.path.exists(target) and not overwrite:
        raise FileError(f"{rel} already exists", 409)
    made = []
    d = pdir
    while not os.path.isdir(d):
        made.append(d)
        d = os.path.dirname(d)
    os.makedirs(pdir, exist_ok=True)
    tmp = target + f".upload-{uuid.uuid4().hex[:6]}"
    size = 0
    try:
        f = open(tmp, "wb")
        try:
            buf = bytearray()
            async for chunk in chunks:
                buf += chunk
                size += len(chunk)
                if len(buf) >= 8 * 2**20:
                    await asyncio.to_thread(f.write, bytes(buf))
                    buf.clear()
            if buf:
                await asyncio.to_thread(f.write, bytes(buf))
        finally:
            f.close()
        if os.path.exists(target):
            _to_trash(sid, target)                           # the replaced file can still be got back
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    own_like(made[-1] if made else target, base)
    return {"path": rel_of(sid, target), "size": size}


# ------------------------------------------------------------------ downloads
class _Sink(io.RawIOBase):
    """Collects what zipfile writes, so a zip can be streamed while it's being made."""

    def __init__(self):
        self.parts: list[bytes] = []
        self.pos = 0

    def writable(self):
        return True

    def write(self, b):
        self.parts.append(bytes(b))
        self.pos += len(b)
        return len(b)

    def tell(self):
        return self.pos

    def take(self) -> bytes:
        out = b"".join(self.parts)
        self.parts.clear()
        return out


def zip_stream(sid: str, rels: list[str]):
    """Zip the given files and folders, yielding the zip as it's made (no temp file, so a
    whole world downloads without first needing that much free space)."""
    paths = [resolve(sid, r) for r in rels]
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", allowZip64=True) as z:
        for p in paths:
            top = os.path.dirname(p)
            walk = [(p, [], [])] if not os.path.isdir(p) else os.walk(p)
            for dirpath, dirnames, filenames in walk:
                items = [dirpath] if not os.path.isdir(p) else [os.path.join(dirpath, f) for f in sorted(filenames)]
                if os.path.isdir(p) and not filenames and not dirnames:   # keep empty folders
                    z.writestr(os.path.relpath(dirpath, top).replace(os.sep, "/") + "/", b"")
                for fp in items:
                    if os.path.islink(fp) or not os.path.isfile(fp):
                        continue
                    arc = os.path.relpath(fp, top).replace(os.sep, "/")
                    kind = zipfile.ZIP_STORED if fp.lower().endswith(STORED) else zipfile.ZIP_DEFLATED
                    zi = zipfile.ZipInfo.from_file(fp, arc)
                    zi.compress_type = kind
                    try:
                        with open(fp, "rb") as src, z.open(zi, "w", force_zip64=True) as dst:
                            for chunk in iter(lambda: src.read(1 << 20), b""):
                                dst.write(chunk)
                                if sink.pos and len(sink.parts) > 4:
                                    yield sink.take()
                    except OSError as e:
                        log.info("[%s] skipped %s in a download: %s", sid, arc, e)
                    yield sink.take()
    yield sink.take()


# ------------------------------------------------------------------ archives
def compress(sid: str, rels: list[str], name: str) -> dict:
    if not rels:
        raise FileError("Pick something to compress")
    name = _valid_name(name if name.lower().endswith(".zip") else name + ".zip")
    first = resolve(sid, rels[0])
    target = os.path.join(os.path.dirname(first), name)
    if os.path.lexists(target):
        raise FileError(f"{name} already exists here", 409)
    tmp = target + ".part"
    try:
        with open(tmp, "wb") as f:
            for part in zip_stream(sid, rels):
                f.write(part)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    own_like(target, root(sid))
    return {"path": rel_of(sid, target), "size": os.path.getsize(target)}


ARCHIVES = (".zip", ".jar", ".mrpack", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")


def extract(sid: str, rel: str, into: str | None = None) -> dict:
    """Unpack a zip or tar archive into a folder named after it (or into a chosen one).
    Entries that would land outside that folder are refused."""
    src = resolve(sid, rel)
    low = src.lower()
    if not low.endswith(ARCHIVES):
        raise FileError("Only .zip and .tar archives can be extracted")
    base = root(sid)
    if into is None:
        stem = os.path.basename(src)
        for ext in sorted(ARCHIVES, key=len, reverse=True):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        dest = os.path.join(os.path.dirname(src), stem or "extracted")
        n = 2
        while os.path.lexists(dest):
            dest = os.path.join(os.path.dirname(src), f"{stem} ({n})")
            n += 1
    else:
        dest = resolve(sid, into, must_exist=False)
    os.makedirs(dest, exist_ok=True)
    real_dest = os.path.realpath(dest)
    count = 0
    if low.endswith((".zip", ".jar", ".mrpack")):
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                out = os.path.realpath(os.path.join(real_dest, name))
                if out != real_dest and not out.startswith(real_dest + os.sep):
                    raise FileError(f"The archive has an unsafe path ({name}); nothing more was extracted")
                if info.is_dir():
                    os.makedirs(out, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(info) as a, open(out, "wb") as b:
                    shutil.copyfileobj(a, b, 1 << 20)
                count += 1
    else:
        with tarfile.open(src) as t:
            members = [m for m in t.getmembers() if m.isfile() or m.isdir()]
            t.extractall(real_dest, members=members, filter="data")
            count = sum(1 for m in members if m.isfile())
    own_like(dest, base)
    return {"path": rel_of(sid, dest), "files": count}


# ------------------------------------------------------------------ search
def search(sid: str, q: str, rel: str = "") -> dict:
    q = (q or "").strip().lower()
    if len(q) < 2:
        raise FileError("Type at least 2 letters to search")
    start = resolve(sid, rel)
    base = root(sid)
    out = []
    t0 = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames.sort()
        for n in dirnames + sorted(filenames):
            if q in n.lower():
                p = os.path.join(dirpath, n)
                e = _entry(p, n)
                e["path"] = rel_of(sid, p)
                out.append(e)
                if len(out) >= SEARCH_LIMIT:
                    return {"results": out, "more": True}
        if time.monotonic() - t0 > 8:
            return {"results": out, "more": True}
    return {"results": out, "more": False, "base": rel_of(sid, start) if start != base else ""}
