"""Player packs: what someone needs on their own computer to join a modded server,
served by SpawnPoint itself from the server's files (no Modrinth or CurseForge account).

  * A Prism Launcher instance (prism.zip) that runs packwiz-installer before every launch,
    so mods added or updated on the server reach players the next time they play.
  * A plain zip (pack.zip) for any other launcher: the mods and client files, plus
    AutoModpack when that's turned on, which keeps them in sync whenever they join.

Everything is behind an unguessable link (/p/<token>), like the public map. Mods are
other people's work: the Settings card says so, since many licences forbid rehosting.

What goes in: the server's mods (minus ones only a server uses), the client-only mods
SpawnPoint turned off on the server, and client folders (config, kubejs, resource packs).
Config files that look like they hold a password, token or webhook are left out."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import secrets
import struct
import threading
import time
import zipfile

from . import config, dockerctl, servers, state

log = logging.getLogger("spawnpoint.playerpack")

# Folders players need besides mods. Only those that exist are included.
CLIENT_DIRS = ("config", "defaultconfigs", "kubejs", "resourcepacks", "shaderpacks", "global_packs", "openloader")
SKIP_IN_DIRS = ("kubejs/server_scripts", "kubejs/exported", "config/automodpack", "config/haproxycompat-common.toml")
# Mods only a server uses (SpawnPoint's helpers among them)
SERVER_ONLY = re.compile(r"(chunky|haproxy|proxy[-_]?protocol|ng[-_]?haproxy|spark|ledger|servercore|ksyxis|"
                         r"discord[-_]?(integration|mc[-_]?chat|chat)|dcintegration|automodpack)", re.I)
SECRET = re.compile(rb"(token|password|passwd|secret|webhook|api[_-]?key|client[_-]?secret)\s*[\"']?\s*[:=]\s*[\"']?"
                    rb"[A-Za-z0-9_\-\.+/]{12,}", re.I)
MAX_FILE = 64 * 2**20                      # bigger files in client folders are skipped (world saves, caches)
AUTOMODPACK_PORT = 25580                   # inside the server's container only; the router forwards to it
BOOTSTRAP = os.environ.get("PACKWIZ_BOOTSTRAP", "/opt/spawnpoint/assets/packwiz-installer-bootstrap.jar")
BOOTSTRAP_URL = "https://github.com/packwiz/packwiz-installer-bootstrap/releases/latest/download/packwiz-installer-bootstrap.jar"

_sha_cache: dict[tuple, str] = {}
_index_cache: dict[str, tuple] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ settings
def sync_active(s: dict) -> bool:
    """AutoModpack is on and its jar is on the server (it arrives at the next start)."""
    return bool(settings(s).get("sync") and automodpack_jar(s))

def settings(s: dict) -> dict:
    return dict(s.get("player_pack") or {})


def enabled(s: dict) -> bool:
    return bool(settings(s).get("enabled") and settings(s).get("token"))


def set_settings(sid: str, on: bool | None = None, new_link: bool = False, sync: bool | None = None) -> dict:
    s = state.get(sid)
    cur = settings(s)
    if new_link or not cur.get("token"):
        cur["token"] = secrets.token_urlsafe(12)
    if on is not None:
        cur["enabled"] = bool(on)
    if sync is not None:
        cur["sync"] = bool(sync)
    state.update(sid, player_pack=cur)
    _index_cache.pop(sid, None)
    return cur


def by_token(token: str) -> dict | None:
    import hmac
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,40}", token or ""):
        return None
    for s in state.servers().values():
        pp = settings(s)
        if pp.get("enabled") and pp.get("token") and hmac.compare_digest(pp["token"], token):
            return s
    return None


# ------------------------------------------------------------------ what goes in
def _sha256(path: str) -> str:
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if key not in _sha_cache:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _sha_cache[key] = h.hexdigest()
    return _sha_cache[key]


_check_cache: dict[tuple, bool] = {}


def _cached(kind: str, path: str, fn) -> bool:
    """Per-file checks (opening a jar, reading a config) remembered until the file changes:
    a big pack has hundreds of each, and the card would wait for all of them every time."""
    try:
        st = os.stat(path)
    except OSError:
        return True
    key = (kind, path, st.st_size, st.st_mtime_ns)
    if key not in _check_cache:
        if len(_check_cache) > 50000:
            _check_cache.clear()
        _check_cache[key] = fn(path)
    return _check_cache[key]


def _fabric_server_only(path: str) -> bool:
    return _cached("fabric", path, _fabric_server_only_now)


def _fabric_server_only_now(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            if "fabric.mod.json" in z.namelist():
                meta = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"), strict=False)
                return meta.get("environment") == "server"
    except Exception:
        pass
    return False


def _has_secret(path: str) -> bool:
    return _cached("secret", path, _has_secret_now)


def _has_secret_now(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return bool(SECRET.search(f.read(2 * 2**20)))
    except OSError:
        return True


def files(s: dict) -> tuple[dict[str, str], list[str]]:
    """{path in the pack: file on disk} and the paths left out (with why)."""
    base = dockerctl.server_dir(s["id"])
    out: dict[str, str] = {}
    left: list[str] = []
    mdir = servers.mods_dir(s)
    mod_folder = os.path.basename(mdir.rstrip("/"))
    if os.path.isdir(mdir):
        for f in sorted(os.listdir(mdir)):
            p = os.path.join(mdir, f)
            if not os.path.isfile(p):
                continue
            if f.endswith(".jar.disabled") and f[:-len(".disabled")] in _auto_disabled(s):
                out[f"mods/{f[:-len('.disabled')]}"] = p          # client-only: off on the server, needed by players
            elif f.endswith(".jar"):
                if f.lower().startswith("automodpack") and settings(s).get("sync"):
                    out[f"mods/{f}"] = p                       # players need it too, to stay in sync
                elif SERVER_ONLY.search(f) or _fabric_server_only(p):
                    left.append(f"{mod_folder}/{f} (only used by the server)")
                else:
                    out[f"mods/{f}"] = p
    for d in CLIENT_DIRS:
        root = os.path.join(base, d)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            for f in sorted(filenames):
                p = os.path.join(dirpath, f)
                rel = os.path.relpath(p, base).replace(os.sep, "/")
                if any(rel == k or rel.startswith(k + "/") for k in SKIP_IN_DIRS) or f.startswith("."):
                    continue
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size > MAX_FILE:
                    left.append(f"{rel} (too big)")
                elif _has_secret(p):
                    left.append(f"{rel} (looks like it holds a password or token)")
                else:
                    out[rel] = p
    return out, left


def _auto_disabled(s: dict) -> set[str]:
    return {x.get("file") if isinstance(x, dict) else x for x in s.get("auto_disabled", [])} - {None}


def loader_info(s: dict) -> tuple[str, str]:
    """The loader and its exact version (like neoforge 21.1.200), read from the server's
    libraries when SpawnPoint wasn't told it (modpacks bring their own)."""
    loader, ver = (s.get("loader") or "").lower(), s.get("loader_version") or ""
    if not ver or ver == s.get("mc_version"):
        try:
            from . import importer
            d = importer.detect(dockerctl.server_dir(s["id"]))
            loader, ver = (d.get("kind") if d.get("kind") in ("neoforge", "forge", "fabric", "quilt") else loader), d.get("loader_version", "")
        except Exception:
            ver = ""
    return loader, ver


# ------------------------------------------------------------------ packwiz
def index(s: dict) -> tuple[bytes, bytes, dict[str, str], list[str]]:
    """(pack.toml, index.toml, files, left out). Cached until the files change."""
    sid = s["id"]
    fl, left = files(s)
    stamp = tuple(sorted((k, os.path.getsize(v), int(os.path.getmtime(v))) for k, v in fl.items()))
    with _lock:
        hit = _index_cache.get(sid)
        if hit and hit[0] == stamp and hit[1] == s.get("mc_version"):
            return hit[2], hit[3], fl, left
    lines = ['hash-format = "sha256"', ""]
    for rel, p in sorted(fl.items()):
        lines += ["[[files]]", f'file = "{_toml(rel)}"', f'hash = "{_sha256(p)}"']
        if not rel.startswith("mods/"):
            lines.append("preserve = true")                 # players' own tweaks to configs survive updates
        lines.append("")
    idx = "\n".join(lines).encode()
    loader, lver = loader_info(s)
    key = {"neoforge": "neoforge", "forge": "forge", "fabric": "fabric", "quilt": "quilt"}.get(loader)
    pack = "\n".join([
        f'name = "{_toml(s.get("name", sid))}"', f'author = "SpawnPoint"',
        f'version = "{time.strftime("%Y.%m.%d", time.gmtime(max((os.path.getmtime(v) for v in fl.values()), default=time.time())))}"',
        'pack-format = "packwiz:1.1.0"', "", "[index]", 'file = "index.toml"', 'hash-format = "sha256"',
        f'hash = "{hashlib.sha256(idx).hexdigest()}"', "", "[versions]", f'minecraft = "{_toml(s.get("mc_version", ""))}"']
        + ([f'{key} = "{_toml(lver)}"'] if key and lver else []) + [""]).encode()
    with _lock:
        _index_cache[sid] = (stamp, s.get("mc_version"), pack, idx)
    return pack, idx, fl, left


def _toml(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


# ------------------------------------------------------------------ downloads
def _servers_dat(name: str, address: str) -> bytes:
    """A multiplayer list with this server in it (uncompressed NBT, like the game writes)."""
    def s_(t: str) -> bytes:
        b = t.encode("utf-8")
        return struct.pack(">H", len(b)) + b
    entry = b"\x08" + s_("name") + s_(name) + b"\x08" + s_("ip") + s_(address) + b"\x00"
    return b"\x0a" + s_("") + b"\x09" + s_("servers") + b"\x0a" + struct.pack(">i", 1) + entry + b"\x00"


def bootstrap_jar() -> bytes:
    for p in (BOOTSTRAP, os.path.join(config.STATE_DIR, "packwiz-installer-bootstrap.jar")):
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    import httpx                                             # not in the image (tests, dev): fetch once
    r = httpx.get(BOOTSTRAP_URL, follow_redirects=True, timeout=60)
    r.raise_for_status()
    p = os.path.join(config.STATE_DIR, "packwiz-installer-bootstrap.jar")
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(p, "wb") as f:
        f.write(r.content)
    return r.content


def prism_zip(s: dict, pack_url: str) -> bytes:
    """A Prism Launcher instance: the right Minecraft and loader, the server in the
    multiplayer list, and packwiz-installer run before every launch."""
    loader, lver = loader_info(s)
    comps = [{"uid": "net.minecraft", "version": s.get("mc_version", ""), "important": True}]
    if loader == "neoforge":
        comps.append({"uid": "net.neoforged", "version": lver})
    elif loader == "forge":
        comps.append({"uid": "net.minecraftforge", "version": lver})
    elif loader in ("fabric", "quilt"):
        comps.append({"uid": "net.fabricmc.intermediary", "version": s.get("mc_version", "")})
        comps.append({"uid": "net.fabricmc.fabric-loader" if loader == "fabric" else "org.quiltmc.quilt-loader", "version": lver})
    name = s.get("name", s["id"])
    cfg = "\n".join([
        "[General]", "ConfigVersion=1.2", "InstanceType=OneSix", f"name={name}", "iconKey=default",
        "OverrideCommands=true",
        f'PreLaunchCommand="$INST_JAVA" -jar packwiz-installer-bootstrap.jar {pack_url}', ""])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("instance.cfg", cfg)
        z.writestr("mmc-pack.json", json.dumps({"formatVersion": 1, "components": comps}, indent=2))
        z.writestr(".minecraft/packwiz-installer-bootstrap.jar", bootstrap_jar())
        z.writestr(".minecraft/servers.dat", _servers_dat(name, s.get("hostname", "")))
    return buf.getvalue()


def plain_zip_files(s: dict) -> list[tuple[str, str | bytes]]:
    """(name in zip, file path or bytes) for the any-launcher zip."""
    _, _, fl, _ = index(s)
    amp = automodpack_jar(s) if sync_active(s) else None
    if amp:                                                   # AutoModpack fetches the rest on first join
        items: list[tuple[str, str | bytes]] = [(f"mods/{os.path.basename(amp)}", amp)]
    else:
        items = sorted(fl.items())
    loader, lver = loader_info(s)
    items.append(("servers.dat", _servers_dat(s.get("name", s["id"]), s.get("hostname", ""))))
    items.append(("READ ME.txt", readme(s, loader, lver).encode()))
    return items


def readme(s: dict, loader: str, lver: str) -> str:
    sync = sync_active(s)
    ld = {"neoforge": "NeoForge", "forge": "Forge", "fabric": "Fabric", "quilt": "Quilt"}.get(loader, loader)
    return "\n".join([
        f"{s.get('name', s['id'])}: files for joining {s.get('hostname', '')}", "",
        f"1. Install Minecraft {s.get('mc_version', '')} with {ld} {lver} in your launcher (or create a profile with it).",
        "2. Copy everything in this zip into that profile's game folder, replacing files if asked.",
        "   servers.dat adds the server to your multiplayer list.",
        f"3. Start the game and join {s.get('hostname', '')}.", "",
        ("This zip only has AutoModpack in it. When you join, it asks you to trust the server, downloads the "
         "server's mods and settings, and restarts the game. After that, joining keeps them up to date." if sync else
         "When the server's mods change, download this zip again."),
        "", "Using Prism Launcher? The Prism download on the same page updates itself every time you play.", ""])


# ------------------------------------------------------------------ AutoModpack on the server
def automodpack_jar(s: dict) -> str | None:
    mdir = servers.mods_dir(s)
    try:
        return next((os.path.join(mdir, f) for f in sorted(os.listdir(mdir))
                     if f.lower().startswith("automodpack") and f.endswith(".jar")), None)
    except OSError:
        return None


def _amp_config(base: str) -> tuple[str, dict | None]:
    p = os.path.join(base, "automodpack", "automodpack-server.json")
    try:
        with open(p, encoding="utf-8") as f:
            return p, json.load(f)
    except (OSError, ValueError):
        return p, None


def automodpack_port(base: str) -> int:
    """Where AutoModpack listens inside the container. Releases up to 4.x share the game's
    port (they spot their own connections by the AMMH greeting); newer ones can be given
    their own port ("connection-mode": "MAGIC"), which SpawnPoint uses when it's there."""
    _, cfg = _amp_config(base)
    return AUTOMODPACK_PORT if cfg and "connection-mode" in cfg else 25565


def configure_automodpack(base: str, real_ip: bool, page_url: str, extra: dict[str, str] | None = None) -> None:
    """AutoModpack's server settings. The router sends connections starting with AMMH to
    it, with the player's real IP the same way the game gets it (AutoModpack checks
    downloads against that address). Client-only files go in host-modpack/main so
    AutoModpack players get them too. AutoModpack writes its settings file on its first
    start; until then there's nothing to change."""
    d = os.path.join(base, "automodpack")
    p, cur = _amp_config(base)
    link = page_url or "https://modrinth.com/project/automodpack"
    if cur is None:
        # AutoModpack hasn't started yet. It makes its own folder on first start; making
        # it here would leave it owned by SpawnPoint and unwritable for the server.
        return
    if cur is not None:
        if "connection-mode" in cur:                           # newer format, kebab-case
            want = {"connection-mode": "MAGIC", "bind-port": AUTOMODPACK_PORT, "advertised-endpoint-port": -1,
                    "accept-proxy-protocol": bool(real_ip), "nag-message": "Get this server's mods to join:",
                    "nag-clickable-message": "Download the player pack", "nag-clickable-link": link}
            stray = []
        else:                                                  # 4.x: on the game's port
            want = {"bindPort": -1, "portToSend": -1, "addressToSend": "", "nagMessage": "Get this server's mods to join:",
                    "nagClickableMessage": "Download the player pack", "nagClickableLink": link}
            stray = [k for k in cur if "-" in k]               # keys an earlier SpawnPoint wrote by mistake
        if stray or any(cur.get(k) != v for k, v in want.items()):
            for k in stray:
                cur.pop(k)
            cur.update(want)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=2)
    _sync_host_files(os.path.join(d, "host-modpack", "main"), extra or {})
    own_like(d, base)


def own_like(path: str, ref: str) -> None:
    from . import files
    files.own_like(path, ref)


def _sync_host_files(host: str, extra: dict[str, str]) -> None:
    """Mirror the extra files into AutoModpack's host folder: hard links where possible
    (no extra disk space), copies otherwise. Files no longer wanted are removed."""
    import shutil
    os.makedirs(host, exist_ok=True)
    for rel, src in extra.items():
        dst = os.path.join(host, rel)
        try:
            st, sd = os.stat(src), (os.stat(dst) if os.path.exists(dst) else None)
            if sd and (sd.st_ino == st.st_ino or (sd.st_size == st.st_size and int(sd.st_mtime) == int(st.st_mtime))):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if sd:
                os.remove(dst)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        except OSError as e:
            log.info("couldn't give AutoModpack %s: %s", rel, e)
    want = {os.path.normpath(r) for r in extra}
    for dirpath, _, filenames in os.walk(host, topdown=False):
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), host)
            if os.path.normpath(rel) not in want:
                try:
                    os.remove(os.path.join(dirpath, f))
                except OSError:
                    pass
        if dirpath != host and not os.listdir(dirpath):
            os.rmdir(dirpath)


def client_only_extras(s: dict) -> dict[str, str]:
    """What AutoModpack should hand out besides what it finds itself (the mods folder and
    KubeJS): the client-only mods SpawnPoint turned off on the server, and the checked
    client files (configs, resource packs, shader packs), minus anything that looked like
    it holds a password. Players' edits to configs are kept (AutoModpack's default)."""
    fl, _ = files(s)
    return {k: v for k, v in fl.items()
            if (k.startswith("mods/") and v.endswith(".disabled")) or not (k.startswith("mods/") or k.startswith("kubejs/"))}


async def set_sync(sid: str, on: bool) -> str:
    """Add or remove AutoModpack on the server. Returns a note for the page."""
    s = state.get(sid)
    if on:
        if automodpack_jar(s) or any(m.get("slug") == "automodpack" for m in s.get("managed_mods", [])):
            set_settings(sid, sync=True)
            return "AutoModpack is already on this server."
        r = await servers.install_mod(s, "automodpack")
        set_settings(sid, sync=True)
        state.update(sid, restart_pending=True)
        return f"Added AutoModpack {r.get('version', '')}. Restart the server to turn it on."
    for m in s.get("managed_mods", []):
        if m.get("slug") == "automodpack":
            servers.remove_managed_mod(sid, m["project_id"])
    jar = automodpack_jar(s)
    if jar:
        os.remove(jar)
    set_settings(sid, sync=False)
    state.update(sid, restart_pending=True)
    return "Removed AutoModpack. Restart the server to finish."


async def repin(s: dict) -> None:
    """Earlier SpawnPoint versions could pin a test build of AutoModpack (5.0 release
    candidates fail to start on some servers). Switch it to the newest stable one."""
    if not settings(s).get("sync"):
        return
    m = next((m for m in s.get("managed_mods", []) if m.get("slug") == "automodpack"), None)
    if not m and automodpack_jar(s):                           # added straight to the mods folder
        f = os.path.basename(automodpack_jar(s))
        m = {"filename": f, "version": f}
    if m and re.search(r"(?i)(rc|beta|alpha|pre)", m.get("version", "")):
        try:
            r = await servers.install_mod(state.get(s["id"]), "automodpack")
            old = os.path.join(servers.mods_dir(s), m.get("filename", ""))
            if m.get("filename") and m["filename"] != r.get("filename") and os.path.isfile(old):
                os.remove(old)                                 # or two copies would clash
            log.info("[%s] AutoModpack %s -> %s", s["id"], m.get("version"), r.get("version"))
        except Exception as e:
            log.warning("[%s] couldn't switch AutoModpack to a stable version: %s", s["id"], e)


def before_boot(s: dict) -> None:
    """Called before each start: keep AutoModpack's settings and extra files current."""
    if not settings(s).get("sync"):
        return
    base = dockerctl.server_dir(s["id"])
    own_like(os.path.join(base, "automodpack"), base)          # repairs a folder an earlier version made
    try:
        configure_automodpack(base, bool(s.get("real_ip")), page_url(s), client_only_extras(s))
    except Exception as e:
        log.warning("[%s] couldn't set up AutoModpack: %s", s["id"], e)


def page_url(s: dict) -> str:
    pp = settings(s)
    return f"{pp.get('base_url', '').rstrip('/')}/p/{pp['token']}" if pp.get("base_url") and pp.get("token") else ""


def pack_url(s: dict) -> str:
    u = page_url(s)
    return f"{u}/pack/pack.toml" if u else ""


def build_zip(s: dict) -> str:
    """The any-launcher zip on disk, rebuilt only when the files change."""
    items = plain_zip_files(s)
    h = hashlib.sha256()
    for name, src in items:
        h.update(name.encode())
        h.update(src if isinstance(src, bytes) else f"{os.path.getsize(src)}:{int(os.path.getmtime(src))}".encode())
    d = os.path.join(config.STATE_DIR, "player-packs")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, f"{s['id']}-{h.hexdigest()[:16]}.zip")
    with _lock:
        if os.path.exists(out):
            return out
        for f in os.listdir(d):                                 # older builds of this server's zip
            if f.startswith(s["id"] + "-"):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
        tmp = out + ".part"
        with zipfile.ZipFile(tmp, "w") as z:
            for name, src in items:
                kind = zipfile.ZIP_STORED if name.endswith(".jar") else zipfile.ZIP_DEFLATED
                if isinstance(src, bytes):
                    z.writestr(name, src, compress_type=kind)
                else:
                    z.write(src, name, compress_type=kind)
        os.replace(tmp, out)
    return out


# ------------------------------------------------------------------ Modrinth App (.mrpack)
_mr_cache: dict[str, dict | None] = {}                       # sha1 -> {"url": ...} or None (not on Modrinth)
MR_HOSTS = ("https://cdn.modrinth.com/",)


def _sha1(path: str) -> str:
    st = os.stat(path)
    key = ("sha1", path, st.st_size, st.st_mtime_ns)
    if key not in _sha_cache:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _sha_cache[key] = h.hexdigest()
    return _sha_cache[key]


def _sha512(path: str) -> str:
    st = os.stat(path)
    key = ("sha512", path, st.st_size, st.st_mtime_ns)
    if key not in _sha_cache:
        h = hashlib.sha512()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _sha_cache[key] = h.hexdigest()
    return _sha_cache[key]


async def modrinth_lookup(hashes: list[str]) -> dict[str, dict | None]:
    """Which files are on Modrinth, by checksum, so the Modrinth App can download those
    from Modrinth itself instead of from this server."""
    from . import catalog
    import httpx
    todo = [h for h in hashes if h not in _mr_cache]
    if todo:
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "SpawnPoint (github.com/z354prance/SpawnPoint)"}) as c:
                r = await c.post(f"{catalog.MR}/version_files", json={"hashes": todo, "algorithm": "sha1"})
                r.raise_for_status()
                found = r.json()
            for h in todo:
                v = found.get(h)
                f = next((x for x in (v or {}).get("files", []) if x.get("hashes", {}).get("sha1") == h), None)
                _mr_cache[h] = {"url": f["url"]} if f and f.get("url", "").startswith(MR_HOSTS) else None
        except Exception as e:                                 # Modrinth unreachable: everything goes in the file
            log.info("Modrinth lookup failed, packing all mods: %s", e)
            return {h: _mr_cache.get(h) for h in hashes}
    return {h: _mr_cache.get(h) for h in hashes}


async def mrpack(s: dict) -> str:
    """A Modrinth App modpack file. Mods published on Modrinth are listed by their
    Modrinth download (so they come from Modrinth); everything else is inside it."""
    import asyncio
    _, _, fl, _ = await asyncio.to_thread(index, s)
    if await asyncio.to_thread(sync_active, s):
        # Just AutoModpack: the Modrinth App sets up the right Minecraft and loader, and
        # AutoModpack brings everything else on the first join.
        amp = automodpack_jar(s)
        fl = {f"mods/{os.path.basename(amp)}": amp}
    mods = {k: v for k, v in fl.items() if k.startswith("mods/")}
    sha1s = {k: await asyncio.to_thread(_sha1, v) for k, v in mods.items()}
    found = await modrinth_lookup(sorted(set(sha1s.values())))
    loader, lver = await asyncio.to_thread(loader_info, s)
    deps = {"minecraft": s.get("mc_version", "")}
    key = {"neoforge": "neoforge", "forge": "forge", "fabric": "fabric-loader", "quilt": "quilt-loader"}.get(loader)
    if key and lver:
        deps[key] = lver
    listed, inside = [], []
    for rel, src in sorted(fl.items()):
        hit = found.get(sha1s.get(rel, ""))
        if hit:
            listed.append({"path": rel, "hashes": {"sha1": sha1s[rel], "sha512": await asyncio.to_thread(_sha512, src)},
                           "env": {"client": "required", "server": "unsupported"},
                           "downloads": [hit["url"]], "fileSize": os.path.getsize(src)})
        else:
            inside.append((rel, src))
    name = s.get("name", s["id"])
    meta = {"formatVersion": 1, "game": "minecraft", "versionId": time.strftime("%Y.%m.%d.%H%M", time.gmtime()),
            "name": name, "summary": f"Everything needed to join {s.get('hostname', '')}",
            "files": listed, "dependencies": deps}
    h = hashlib.sha256(json.dumps(meta["files"], sort_keys=True).encode())
    for rel, src in inside:
        h.update(f"{rel}:{os.path.getsize(src)}:{int(os.path.getmtime(src))}".encode())
    h.update(json.dumps(deps, sort_keys=True).encode())
    d = os.path.join(config.STATE_DIR, "player-packs")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, f"{s['id']}-mr-{h.hexdigest()[:16]}.mrpack")
    if os.path.exists(out):
        return out
    def build():
        with _lock:
            for f in os.listdir(d):
                if f.startswith(s["id"] + "-mr-"):
                    try:
                        os.remove(os.path.join(d, f))
                    except OSError:
                        pass
            tmp = out + ".part"
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("modrinth.index.json", json.dumps(meta, indent=2))
                for rel, src in inside:
                    z.write(src, f"overrides/{rel}", compress_type=zipfile.ZIP_STORED if rel.endswith(".jar") else zipfile.ZIP_DEFLATED)
                z.writestr("overrides/servers.dat", _servers_dat(name, s.get("hostname", "")))
            os.replace(tmp, out)
    await asyncio.to_thread(build)
    return out
