"""SpawnPoint: a MintServers-style Minecraft panel for one Unraid box."""
import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import activity, alerts, backups, bedrock, catalog, lagwatch, sharedlist, heads, packs, schedules, hostinfo, mapper, config, dockerctl, rcon, router, servers, state, topology, users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("spawnpoint")
STATIC = os.path.join(os.path.dirname(__file__), "static")
_stats: dict[str, dict] = {}


# ------------------------------------------------------------------ background loops
async def stats_loop():
    """Refresh RAM/CPU/players/TPS for running servers; handle auto-sleep."""
    idle_since: dict[str, float] = {}
    joined: dict[str, dict[str, float]] = {}     # sid -> player -> first seen online
    while True:
        for sid, s in list(state.servers().items()):
            try:
                st = await asyncio.to_thread(dockerctl.status, sid)
                entry = {"status": st, "mem_gb": 0, "cpu": 0, "players": [], "online": 0, "max": 0, "mspt": None}
                if st == "running" and s.get("phase") in ("online", "pregen-spawn"):
                    entry["mem_gb"] = round(await asyncio.to_thread(dockerctl.mem_usage_bytes, sid) / 2**30, 2)
                    entry["cpu"] = await asyncio.to_thread(dockerctl.cpu_percent, sid)
                    try:
                        n, mx, names = rcon.parse_players(await servers.arcmd(s, "list"))
                        seen = joined.setdefault(sid, {})
                        now = time.time()
                        for nm in names:
                            seen.setdefault(nm, now)
                        for nm in list(seen):
                            if nm not in names:
                                del seen[nm]
                        entry.update(online=n, max=mx, players=names, joined=dict(seen))
                        if s["pregen"]["stage"] == "background":
                            entry["mspt"], entry["p95"] = s["pregen"].get("mspt"), s["pregen"].get("p95")
                        else:
                            t = rcon.parse_tick(await servers.arcmd(s, "tick query"))
                            entry["mspt"], entry["p95"] = (t["avg"], t["p95"]) if t else (None, None)
                    except rcon.RconError:
                        pass
                    mins = s.get("auto_sleep_min") or 0
                    if mins and s.get("phase") == "online" and s["pregen"]["stage"] == "done":
                        if entry["online"] == 0:
                            idle_since.setdefault(sid, time.time())
                            if time.time() - idle_since[sid] > mins * 60:
                                log.info("[%s] idle %d min, sleeping", sid, mins)
                                await servers.stop(sid)
                                state.update(sid, phase="sleeping", phase_msg="Stopped after being empty")
                                idle_since.pop(sid, None)
                        else:
                            idle_since.pop(sid, None)
                if st != "running" and s.get("phase") == "online":
                    await servers.check_crash(sid)
                if st == "running":
                    entry["started_at"] = await asyncio.to_thread(dockerctl.started_at, sid)
                    if s.get("phase") == "online":
                        lagwatch.note(sid, entry.get("p95"), entry.get("mspt"), entry.get("online"))
                    hostinfo.record(sid, entry["mspt"], entry["mem_gb"], entry["cpu"], entry.get("p95"),
                                    entry.get("online") if s.get("phase") == "online" else None)
                _stats[sid] = entry
            except Exception as e:
                log.debug("stats %s: %s", sid, e)
        hostinfo.maybe_refresh_sizes()
        await asyncio.to_thread(hostinfo.check_disks)
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.load()
    users.ensure_bootstrap()
    topology.allocator()
    try:
        await asyncio.to_thread(dockerctl.ensure_network)
        await servers.resume_all()
    except Exception as e:
        log.warning("resume failed (is the docker socket mounted?): %s", e)
    task = asyncio.create_task(stats_loop())
    maps = asyncio.create_task(mapper.loop(lambda sid: _stats.get(sid, {}).get("mspt")))
    sched = asyncio.create_task(schedules.loop())
    await bedrock.start_all()
    bsweep = asyncio.create_task(bedrock.sweep_loop())
    packcheck = asyncio.create_task(packs.loop())
    from . import proxy
    mc = await proxy.serve()
    yield
    mc.close()
    task.cancel()
    maps.cancel()
    sched.cancel()
    bsweep.cancel()
    bedrock.stop_all()
    packcheck.cancel()
    schedules.stop_all()
    mapper.stop_all()
    await servers.shutdown_all()


app = FastAPI(title="SpawnPoint", lifespan=lifespan)


# ------------------------------------------------------------------ auth, roles, activity
def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers, so behind Pangolin this is the real client.
    return request.client.host if request.client else ""


def require_auth(request: Request) -> dict:
    found = users.check_token(request.cookies.get("sp_session", ""))
    if not found:
        raise HTTPException(401, "Not logged in")
    name, u = found
    me = {"name": name, "role": u["role"]}
    request.state.user = me
    return me


def need(role: str):
    """Dependency: signed in with at least this role (viewer < moderator < admin)."""
    def dep(request: Request, me: dict = Depends(require_auth)) -> dict:
        request.state.user = me
        if users.RANK.get(me["role"], -1) < users.RANK[role]:
            raise HTTPException(403, f"Your role ({me['role']}) can't do this; it needs {role}.")
        return me
    return dep


viewer = [Depends(need("viewer"))]
moderator = [Depends(need("moderator"))]
admin = [Depends(need("admin"))]


def record(request: Request, action: str, target: str = "", detail: str = "", ok: bool = True) -> None:
    me = getattr(request.state, "user", None) or {}
    activity.record(me.get("name"), me.get("role"), client_ip(request), action, target, detail, ok)


class Login(BaseModel):
    username: str = "admin"
    password: str


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    ip = client_ip(request)
    name = body.username.strip()
    if users.blocked(ip, name):
        activity.record(name, "", ip, "login.blocked", ok=False)
        raise HTTPException(429, "Too many failed sign-ins. Try again in 10 minutes.")
    u = users.authenticate(name, body.password)
    if not u:
        users.note_fail(ip, name)
        activity.record(name, "", ip, "login.failed", ok=False)
        time.sleep(1)
        raise HTTPException(401, "Wrong username or password")
    users.clear_fails(ip, name)
    users.note_login(name, ip)
    activity.record(name, u["role"], ip, "login")
    response.set_cookie("sp_session", users.make_token(name), httponly=True, samesite="lax",
                        max_age=users.SESSION_DAYS * 86400)
    return {"ok": True, "username": name, "role": u["role"]}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    found = users.check_token(request.cookies.get("sp_session", ""))
    if found:
        activity.record(found[0], found[1]["role"], client_ip(request), "logout")
    response.delete_cookie("sp_session")
    return {"ok": True}


@app.get("/api/me", dependencies=viewer)
def me(request: Request):
    u = request.state.user
    rec = users.get(u["name"]) or {}
    return {**u, "must_change_password": bool(rec.get("bootstrap"))}


class PasswordChange(BaseModel):
    current: str
    new: str


@app.post("/api/me/password", dependencies=viewer)
def change_my_password(body: PasswordChange, request: Request, response: Response):
    name = request.state.user["name"]
    if not users.authenticate(name, body.current):
        record(request, "user.password", name, "wrong current password", ok=False)
        raise HTTPException(400, "Your current password is wrong")
    try:
        users.update(name, password=body.new)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    record(request, "user.password", name, "changed own password")
    response.set_cookie("sp_session", users.make_token(name), httponly=True, samesite="lax",
                        max_age=users.SESSION_DAYS * 86400)
    return {"ok": True}


# ------------------------------------------------------------------ users (admin)
class NewUser(BaseModel):
    username: str
    password: str
    role: str


class EditUser(BaseModel):
    role: str | None = None
    password: str | None = None


@app.get("/api/users", dependencies=admin)
def list_users():
    return users.list_users()


@app.post("/api/users", dependencies=admin)
def add_user(body: NewUser, request: Request):
    try:
        users.create(body.username.strip(), body.password, body.role)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    record(request, "user.create", body.username.strip(), f"role {body.role}")
    return {"ok": True}


@app.patch("/api/users/{name}", dependencies=admin)
def edit_user(name: str, body: EditUser, request: Request):
    try:
        users.update(name, role=body.role, password=body.password)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    parts = ([f"role → {body.role}"] if body.role else []) + (["password reset"] if body.password else [])
    record(request, "user.update", name, ", ".join(parts))
    return {"ok": True}


@app.delete("/api/users/{name}", dependencies=admin)
def remove_user(name: str, request: Request):
    if name == request.state.user["name"]:
        raise HTTPException(400, "You can't delete your own account")
    try:
        users.delete(name)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    record(request, "user.delete", name)
    return {"ok": True}


@app.get("/api/activity", dependencies=admin)
def get_activity(limit: int = 200, user: str = "", action: str = "", target: str = "", before: float | None = None):
    return activity.read(min(max(limit, 1), 1000), user, action, target, before)


# ------------------------------------------------------------------ activity middleware
import re as _re
_ACTIONS = [
    ("POST", r"^/api/servers$", "server.create"),
    ("PUT", r"^/api/import$", "server.import"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/action/(?P<a>[^/]+)$", "server.{a}"),
    ("DELETE", r"^/api/servers/(?P<sid>[^/]+)$", "server.delete"),
    ("PATCH", r"^/api/servers/(?P<sid>[^/]+)$", "server.settings"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/command$", "console.command"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/players$", "players.change"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/invite-only$", "players.invite-only"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/mods$", "mods.add"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/mods/file$", "mods.upload"),
    ("DELETE", r"^/api/servers/(?P<sid>[^/]+)/mods/managed/(?P<d>[^/]+)$", "mods.remove"),
    ("PUT", r"^/api/servers/(?P<sid>[^/]+)/properties$", "server.properties"),
    ("PUT", r"^/api/servers/(?P<sid>[^/]+)/files$", "files.edit"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/backups$", "backup.create"),
    ("PUT", r"^/api/servers/(?P<sid>[^/]+)/backups/keep$", "backup.retention"),
    ("PUT", r"^/api/servers/(?P<sid>[^/]+)/schedule$", "schedule.change"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/pack/update$", "pack.update"),
    ("PUT", r"^/api/alerts$", "alerts.change"),
    ("PUT", r"^/api/servers/(?P<sid>[^/]+)/public-map$", "map.public"),
    ("PUT", r"^/api/servers/(?P<sid>[^/]+)/bedrock$", "server.bedrock"),
    ("POST", r"^/api/shared-players$", "players.shared"),
    ("DELETE", r"^/api/shared-players/(?P<d>[^/]+)$", "players.shared"),
    ("PUT", r"^/api/servers/(?P<sid>[^/]+)/shared-list$", "players.shared"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/pack/rollback$", "pack.rollback"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/backups/(?P<d>[^/]+)/restore$", "backup.restore"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/backups/(?P<d>[^/]+)/restore-files$", "backup.restore-files"),
    ("GET", r"^/api/servers/(?P<sid>[^/]+)/backups/(?P<d>[^/]+)$", "backup.download"),
    ("DELETE", r"^/api/servers/(?P<sid>[^/]+)/backups/(?P<d>[^/]+)$", "backup.delete"),
    ("POST", r"^/api/servers/(?P<sid>[^/]+)/map/render$", "map.render"),
]
_ACTIONS = [(m, _re.compile(p), a) for m, p, a in _ACTIONS]


@app.middleware("http")
async def log_activity(request: Request, call_next):
    response = await call_next(request)
    for method, pat, action in _ACTIONS:
        if request.method != method:
            continue
        m = pat.match(request.url.path)
        if not m:
            continue
        g = m.groupdict()
        me = getattr(request.state, "user", None) or {}
        if not me and response.status_code == 401:
            break                                  # not signed in: nothing to attribute
        detail = getattr(request.state, "detail", "") or g.get("d", "")
        if response.status_code == 403:
            detail = "not allowed for this role"
        activity.record(me.get("name"), me.get("role"), client_ip(request), action.format(a=g.get("a", "")),
                        getattr(request.state, "target", "") or g.get("sid", ""), detail,
                        ok=response.status_code < 400)
        break
    return response


# ------------------------------------------------------------------ helpers
def _get(sid: str) -> dict:
    try:
        return state.get(sid)
    except KeyError:
        raise HTTPException(404, "No such server")


def _public(s: dict) -> dict:
    out = {k: v for k, v in s.items() if k not in ("rcon_password",)}
    out["stats"] = _stats.get(s["id"], {})
    out["cpus"] = topology.allocator().cpus_for(s["id"])
    out["burst"] = bool(topology.allocator().burst.get(s["id"]))
    from . import perf as _perf
    out["mem_limit_gb"] = _perf.container_limit_gb(s["memory"]["max_gb"], s.get("mod_count"))
    out["mem_normal_gb"] = dockerctl.normal_gb(s)
    out["world_gb"] = hostinfo.server_gb(s["id"])
    out["spawn"] = dict(zip(("x", "z"), mapper.spawn_point(s["id"])))
    out["map_ready"] = mapper.has_map(s["id"])
    try:
        out["progress"] = servers.install_progress(s)
    except Exception:
        out["progress"] = None
    return out


def _safe_path(sid: str, rel: str) -> str:
    root = os.path.realpath(dockerctl.server_dir(sid))
    p = os.path.realpath(os.path.join(root, rel.lstrip("/")))
    if p != root and not p.startswith(root + os.sep):
        raise HTTPException(400, "Path outside server folder")
    return p


# ------------------------------------------------------------------ info
@app.get("/api/info", dependencies=viewer)
def info():
    pool = []
    for sid, s in state.servers().items():
        st = _stats.get(sid, {})
        pool.append({"id": sid, "name": s.get("name", sid), "mem_gb": st.get("mem_gb", 0),
                     "normal_gb": dockerctl.normal_gb(s), "max_gb": s["memory"]["max_gb"]})
    online = sum(v.get("online", 0) for v in _stats.values())
    slots = sum(v.get("max", 0) for v in _stats.values() if v.get("status") == "running")
    return {"base_domain": config.BASE_DOMAIN, "ram_pool_gb": config.ram_pool_gb(), "lag_report_ms": config.LAG_REPORT_MS,
            "ram_pool_used_gb": round(sum(v.get("mem_gb", 0) for v in _stats.values()), 1),
            "pool": pool, "players": {"online": online, "slots": slots},
            "host": {"memory": hostinfo.meminfo(), "disk": hostinfo.disk(), "guard": hostinfo.disk_guard()},
            "engine": dockerctl.engine_info(),
            "curseforge": bool(config.CF_API_KEY), "topology": topology.allocator().summary(),
            "pregen": {"spawn": config.SPAWN_PREGEN_RADIUS, "full": config.FULL_PREGEN_RADIUS}}


@app.get("/api/servers/{sid}/history", dependencies=viewer)
def server_history(sid: str, period: str = Query("1h", alias="range")):
    _get(sid)
    if period == "1h":
        return [{"t": t, "mspt": m, "p95": p, "mem_gb": g, "cpu": c, "players": n} for t, m, g, c, p, n in hostinfo.history(sid)]
    span = {"24h": 86400, "7d": 7 * 86400}.get(period)
    if not span:
        raise HTTPException(422, "range is 1h, 24h or 7d")
    rows = hostinfo.long_history(sid, time.time() - span)
    step = 6 if period == "7d" else 1                     # 7 days: one point per 30 minutes
    out = []
    for i in range(0, len(rows), step):
        g = rows[i:i + step]
        pick = lambda j, f: f([r[j] for r in g if r[j] is not None]) if any(r[j] is not None for r in g) else None
        out.append({"t": g[0][0], "mspt": pick(1, lambda v: round(sum(v) / len(v), 2)), "p95": pick(2, max),
                    "mem_gb": pick(3, lambda v: round(sum(v) / len(v), 2)), "mem_peak": pick(4, max),
                    "cpu": pick(5, lambda v: round(sum(v) / len(v), 1)), "players": pick(6, max)})
    return out


@app.get("/api/versions", dependencies=moderator)
async def versions():
    try:
        return await catalog.mc_versions()
    except Exception:
        return ["1.21.10", "1.21.8", "1.21.1", "1.20.1", "1.19.2", "1.18.2", "1.16.5", "1.12.2"]


@app.get("/api/modpacks", dependencies=moderator)
async def modpacks(q: str = "", source: str = "modrinth"):
    try:
        return await catalog.search_modpacks(q, source)
    except Exception as e:
        raise HTTPException(502, f"Catalog error: {e}")


# ------------------------------------------------------------------ servers
@app.get("/api/servers", dependencies=viewer)
def list_servers():
    return sorted((_public(s) for s in state.servers().values()), key=lambda s: s["created"])


class CreateReq(BaseModel):
    kind: str  # vanilla|paper|purpur|fabric|forge|neoforge|quilt|modrinth|curseforge|ftb
    name: str = ""
    mc_version: str = ""
    project: str = ""
    title: str = ""
    version_id: str | None = None
    java_tag: str | None = None
    seed: str = ""


@app.post("/api/servers", dependencies=moderator)
async def create_server(req: CreateReq, request: Request):
    if req.kind in ("modrinth", "curseforge", "ftb") and not req.project:
        raise HTTPException(400, "Pick a modpack")
    if req.kind == "curseforge" and not config.CF_API_KEY:
        raise HTTPException(400, "Set CF_API_KEY to use CurseForge packs")
    if len(req.seed.strip()) > 64 or "\n" in req.seed:
        raise HTTPException(422, "A seed is a number or a short word")
    try:
        s = await servers.create(req.model_dump())
    except Exception as e:
        raise HTTPException(502, str(e))
    request.state.target = s["id"]
    request.state.detail = f"{req.kind} {req.project or req.mc_version or ''}".strip()
    return _public(s)


@app.put("/api/import", dependencies=moderator)
async def import_server(request: Request, name: str = "", filename: str = "", kind: str = "", mc_version: str = ""):
    """Upload a .zip / .tar.gz of an existing server (raw request body, streamed to disk)."""
    import secrets as _secrets
    import shutil as _shutil
    from . import importer
    low = filename.lower()
    if not low.endswith((".zip", ".tar.gz", ".tgz", ".tar")):
        raise HTTPException(400, "Upload a .zip or .tar.gz of the server folder.")
    if kind and kind not in importer.KINDS:
        raise HTTPException(400, "Unknown server type")
    os.makedirs(importer.UPLOAD_DIR, exist_ok=True)
    size = int(request.headers.get("content-length") or 0)
    free = _shutil.disk_usage(importer.UPLOAD_DIR).free
    if size and size * 2.2 > free:
        raise HTTPException(507, f"Not enough disk space for this upload and its unpacked copy "
                                 f"({free / 2**30:.1f} GB free).")
    ext = ".zip" if low.endswith(".zip") else ".tar.gz"
    path = os.path.join(importer.UPLOAD_DIR, _secrets.token_hex(8) + ext)
    try:
        with open(path, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise HTTPException(400, "Upload was interrupted.")
    if os.path.getsize(path) == 0:
        os.remove(path)
        raise HTTPException(400, "The upload was empty.")
    s = await importer.start_import(path, name or re.sub(r"\.(zip|tar\.gz|tgz|tar)$", "", filename, flags=re.I),
                                    kind, mc_version)
    return _public(s)


@app.get("/api/servers/{sid}", dependencies=viewer)
def get_server(sid: str):
    return _public(_get(sid))


@app.post("/api/servers/{sid}/action/{action}", dependencies=moderator)
async def server_action(sid: str, action: str):
    _get(sid)
    try:
        if action == "start":
            await servers.start(sid)
        elif action == "stop":
            await servers.stop(sid)
        elif action == "kill":
            await servers.kill(sid)
        elif action == "restart":
            await servers.stop(sid)
            await servers.start(sid)
        elif action == "pregen-pause":
            from . import pregen
            # remembered, so the automatic resume (and a restart) doesn't undo it
            pregen._pg(sid, paused=True, held=True, paused_for="hand")
            if state.get(sid)["pregen"].get("method") != "forceload":
                await pregen._chunky(state.get(sid), "pause")
        elif action == "pregen-resume":
            from . import pregen
            pregen._pg(sid, paused=False, held=False, paused_for=None)
            if state.get(sid)["pregen"].get("method") != "forceload":
                await pregen._chunky(state.get(sid), "continue")
        else:
            raise HTTPException(404, "Unknown action")
    except ValueError as e:
        raise HTTPException(409, str(e))
    except rcon.RconError as e:
        raise HTTPException(409, f"Server not reachable: {e}")
    return _public(state.get(sid))


@app.delete("/api/servers/{sid}", dependencies=admin)
async def delete_server(sid: str, files: bool = False):
    _get(sid)
    await servers.delete(sid, files)
    await asyncio.to_thread(mapper.cleanup, sid)
    hostinfo.forget(sid)
    return {"ok": True}


class Settings(BaseModel):
    name: str | None = None
    hostname_prefix: str | None = None
    max_gb: int | None = None
    soft_gb: int | None = None
    init_gb: int | None = None
    auto_memory: bool | None = None
    auto_sleep_min: int | None = None
    auto_restart: bool | None = None
    lag_reports: bool | None = None
    pregen_pause_when_online: bool | None = None
    java_tag: str | None = None
    full_radius: int | None = None
    real_ip: bool | None = None


@app.patch("/api/servers/{sid}", dependencies=moderator)
async def update_settings(sid: str, body: Settings):
    s = _get(sid)
    changes, restart_needed = {}, False
    if body.name:
        changes["name"] = body.name
    if body.auto_sleep_min is not None:
        changes["auto_sleep_min"] = max(0, body.auto_sleep_min)
    if body.auto_restart is not None:
        changes["auto_restart"] = body.auto_restart
    if body.lag_reports is not None:
        changes["lag_reports"] = body.lag_reports
    if body.pregen_pause_when_online is not None:
        changes["pregen_pause_when_online"] = body.pregen_pause_when_online
    if body.hostname_prefix:
        new_host = f"{state.slugify(body.hostname_prefix)}.{config.BASE_DOMAIN}"
        if new_host != s["hostname"]:
            if any(x["hostname"] == new_host for x in state.servers().values()):
                raise HTTPException(409, "Another server already uses that address")
            changes["hostname"] = new_host
    mem = dict(s["memory"])
    if body.auto_memory is not None:
        mem["auto"] = body.auto_memory
    if body.max_gb:
        mem.update(max_gb=max(2, body.max_gb), auto=False)
        restart_needed = True
    if body.init_gb:
        mem.update(init_gb=max(1, body.init_gb), auto=False)
        restart_needed = True
    if body.soft_gb:
        mem.update(soft_gb=max(1, body.soft_gb), auto=False)
        restart_needed = True
    if not mem.get("auto"):
        mem["soft_gb"] = min(mem.get("soft_gb") or mem["max_gb"], mem["max_gb"])
        mem["init_gb"] = min(mem["init_gb"], mem["soft_gb"])
    changes["memory"] = mem
    if body.java_tag and body.java_tag != s["java_tag"]:
        changes["java_tag"] = body.java_tag
        restart_needed = True
    if body.real_ip is not None:
        changes["real_ip_disabled"] = not body.real_ip
        restart_needed = True
    if body.full_radius:
        s["pregen"]["full_radius"] = body.full_radius
        if s["pregen"]["stage"] == "done":
            s["pregen"]["stage"] = "background"
        changes["pregen"] = s["pregen"]
    s = state.update(sid, **changes)
    return {**_public(s), "restart_needed": restart_needed}


# ------------------------------------------------------------------ console
class Cmd(BaseModel):
    command: str


@app.post("/api/servers/{sid}/command", dependencies=moderator)
async def run_command(sid: str, body: Cmd, request: Request):
    s = _get(sid)
    request.state.detail = body.command.lstrip("/")
    try:
        return {"output": await servers.arcmd(s, body.command.lstrip("/"))}
    except rcon.RconError as e:
        raise HTTPException(409, f"Server not reachable: {e}")


_NOISE = ("Thread RCON Client", "RCON Listener #", "[RconClient]", "Rcon connection from")


def _noise(line: str) -> bool:
    """SpawnPoint's own status checks over RCON: not interesting in the console."""
    return any(n in line for n in _NOISE)


@app.get("/api/servers/{sid}/console", dependencies=viewer)
async def console(sid: str, request: Request):
    _get(sid)

    async def gen():
        for line in await asyncio.to_thread(dockerctl.recent_logs, sid, 300):
            if not _noise(line):
                yield f"data: {line}\n\n"
        h = dockerctl.hub(sid)
        h.ensure_running()
        seq = h.seq
        while not await request.is_disconnected():
            new = h.since(seq)
            for n, line in new:
                seq = n
                if not _noise(line):
                    yield f"data: {line}\n\n"
            if not new:
                yield ": ping\n\n"
                await asyncio.sleep(1)
            h = dockerctl.hub(sid)  # may have been reset by a restart
            if h.seq < seq:
                seq = 0
            h.ensure_running()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ players
class Player(BaseModel):
    name: str
    action: str  # whitelist-add|whitelist-remove|op|deop|kick|ban|pardon


@app.post("/api/servers/{sid}/players", dependencies=moderator)
async def player_action(sid: str, body: Player):
    s = _get(sid)
    cmds = {"whitelist-add": "whitelist add", "whitelist-remove": "whitelist remove", "op": "op",
            "deop": "deop", "kick": "kick", "ban": "ban", "pardon": "pardon"}
    if body.action not in cmds or not body.name.replace("_", "").isalnum():
        raise HTTPException(400, "Bad request")
    try:
        return {"output": await servers.arcmd(s, f"{cmds[body.action]} {body.name}")}
    except rcon.RconError as e:
        raise HTTPException(409, f"Server not reachable: {e}")


class InviteOnly(BaseModel):
    on: bool


@app.post("/api/servers/{sid}/invite-only", dependencies=moderator)
async def invite_only(sid: str, body: InviteOnly):
    """Minecraft has no server password; the whitelist is how you make a server private.
    Applies live when the server is running and is saved for future starts."""
    s = _get(sid)
    v = "true" if body.on else "false"
    servers.write_properties(sid, {"white-list": v, "enforce-whitelist": v})
    live = False
    if dockerctl.status(sid) == "running":
        try:
            await servers.arcmd(s, "whitelist on" if body.on else "whitelist off")
            live = True
        except rcon.RconError:
            pass
    return {"on": body.on, "applied_now": live}


# ------------------------------------------------------------------ shared invite list (optional)
@app.get("/api/shared-players", dependencies=viewer)
def shared_players():
    return {"players": sharedlist.players(),
            "servers": {sid: sharedlist.settings(s) for sid, s in state.servers().items()}}


class SharedNames(BaseModel):
    names: list[str] = []
    import_from: str = ""


@app.post("/api/shared-players", dependencies=moderator)
async def shared_add(body: SharedNames):
    try:
        if body.import_from:
            _get(body.import_from)
            n = sharedlist.import_from(body.import_from)
        else:
            sharedlist.add(body.names)
            n = len(body.names)
    except ValueError as e:
        raise HTTPException(422, str(e))
    asyncio.create_task(sharedlist.sync_all())
    return {"added": n, "players": sharedlist.players()}


@app.delete("/api/shared-players/{name}", dependencies=moderator)
async def shared_remove(name: str):
    sharedlist.remove(name)
    asyncio.create_task(sharedlist.sync_all())
    return {"players": sharedlist.players()}


class SharedServer(BaseModel):
    enabled: bool | None = None
    exclude: list[str] | None = None


@app.put("/api/servers/{sid}/shared-list", dependencies=moderator)
async def shared_server(sid: str, body: SharedServer):
    _get(sid)
    out = sharedlist.set_server(sid, body.enabled, body.exclude)
    r = await sharedlist.sync(sid)
    return {**sharedlist.settings(state.get(sid)), "changed": r}


@app.get("/api/servers/{sid}/lists", dependencies=viewer)
def player_lists(sid: str):
    import json
    _get(sid)
    out = {}
    for key in ("whitelist", "ops", "banned-players"):
        p = os.path.join(dockerctl.server_dir(sid), f"{key}.json")
        try:
            with open(p) as f:
                out[key] = [e.get("name") for e in json.load(f)]
        except (OSError, ValueError):
            out[key] = []
    out["invite_only"] = dict(servers.read_properties(sid)).get("white-list", "false") == "true"
    return out


# ------------------------------------------------------------------ mods
@app.get("/api/servers/{sid}/mods", dependencies=viewer)
def list_mods(sid: str):
    s = _get(sid)
    return {"loader": s.get("loader"), "mc_version": s.get("mc_version"), "files": servers.mod_files(s),
            "managed": s.get("managed_mods", []), "is_pack": servers.is_managed(s),
            "folder": os.path.basename(servers.mods_dir(s))}


@app.get("/api/servers/{sid}/mods/search", dependencies=moderator)
async def search_mods(sid: str, q: str = ""):
    s = _get(sid)
    if not s.get("loader") or s["loader"] == "vanilla":
        raise HTTPException(400, "Vanilla servers don't take mods. Create a Fabric, NeoForge or Paper server.")
    try:
        return await catalog.search_mods(q, s["loader"], s.get("mc_version", ""))
    except Exception as e:
        raise HTTPException(502, f"Catalog error: {e}")


class AddMod(BaseModel):
    project: str


@app.post("/api/servers/{sid}/mods", dependencies=moderator)
async def add_mod(sid: str, body: AddMod):
    s = _get(sid)
    try:
        r = await servers.install_mod(s, body.project)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, str(e))
    state.update(sid, mod_count=len(servers.mod_files(s)), restart_pending=True)
    return {**r, "restart_needed": True}


@app.delete("/api/servers/{sid}/mods/managed/{project_id}", dependencies=moderator)
def remove_managed(sid: str, project_id: str):
    _get(sid)
    servers.remove_managed_mod(sid, project_id)
    state.update(sid, restart_pending=True)
    return {"ok": True, "restart_needed": True}


class ModToggle(BaseModel):
    filename: str
    action: str  # disable|enable|delete


@app.post("/api/servers/{sid}/mods/file", dependencies=moderator)
def mod_file(sid: str, body: ModToggle):
    s = _get(sid)
    d = servers.mods_dir(s)
    name = os.path.basename(body.filename)
    p = os.path.join(d, name)
    if not os.path.exists(p):
        raise HTTPException(404, "No such file")
    if body.action == "delete":
        os.remove(p)
    elif body.action == "disable" and name.endswith(".jar"):
        os.rename(p, p + ".disabled")
    elif body.action == "enable" and name.endswith(".disabled"):
        os.rename(p, p[: -len(".disabled")])
    state.update(sid, restart_pending=True)
    return {"files": servers.mod_files(s), "restart_needed": True}


# ------------------------------------------------------------------ properties
@app.get("/api/servers/{sid}/properties", dependencies=moderator)
def get_props(sid: str):
    _get(sid)
    return servers.read_properties(sid)


@app.put("/api/servers/{sid}/properties", dependencies=moderator)
def put_props(sid: str, values: dict[str, str]):
    _get(sid)
    servers.write_properties(sid, values)
    state.update(sid, restart_pending=True)
    return {"ok": True, "restart_needed": True}


# ------------------------------------------------------------------ files
@app.get("/api/servers/{sid}/files", dependencies=moderator)
def list_files(sid: str, path: str = ""):
    _get(sid)
    p = _safe_path(sid, path)
    if os.path.isdir(p):
        entries = []
        for n in sorted(os.listdir(p), key=lambda x: (not os.path.isdir(os.path.join(p, x)), x.lower())):
            fp = os.path.join(p, n)
            entries.append({"name": n, "dir": os.path.isdir(fp),
                            "size": os.path.getsize(fp) if os.path.isfile(fp) else None})
        return {"path": path, "entries": entries}
    if os.path.getsize(p) > 2 * 2**20:
        raise HTTPException(413, "File too large to edit here")
    with open(p, encoding="utf-8", errors="replace") as f:
        return {"path": path, "content": f.read()}


class FileWrite(BaseModel):
    path: str
    content: str


@app.put("/api/servers/{sid}/files", dependencies=moderator)
def write_file(sid: str, body: FileWrite):
    _get(sid)
    p = _safe_path(sid, body.path)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body.content)
    return {"ok": True}


# ------------------------------------------------------------------ backups
@app.get("/api/servers/{sid}/backups", dependencies=viewer)
async def list_backups(sid: str):
    s = _get(sid)
    return {"backups": await backups.list_all(sid), "keep": backups.keep_rules(s), "status": s.get("backup") or {},
            "store_bytes": await backups.store_size()}


@app.post("/api/servers/{sid}/backups", dependencies=moderator)
async def make_backup(sid: str):
    _get(sid)
    try:
        return await backups.create(sid, "manual")
    except backups.BackupError as e:
        raise HTTPException(409, str(e))


class Keep(BaseModel):
    last: int = 0
    hourly: int = 0
    daily: int = 0
    weekly: int = 0
    monthly: int = 0


@app.put("/api/servers/{sid}/backups/keep", dependencies=moderator)
async def set_keep(sid: str, body: Keep):
    _get(sid)
    keep = {k: max(0, min(1000, v)) for k, v in body.model_dump().items()}
    if not any(keep.values()):
        raise HTTPException(422, "Keep at least one backup")
    state.update(sid, backup_keep=keep)
    return {"keep": keep}


@app.post("/api/servers/{sid}/backups/{name}/restore", dependencies=moderator)
async def restore_backup(sid: str, name: str):
    _get(sid)
    try:
        await backups.restore(sid, name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except backups.BackupError as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@app.get("/api/servers/{sid}/backups/{name}/files", dependencies=moderator)
async def backup_files(sid: str, name: str, path: str = "/"):
    _get(sid)
    try:
        return await backups.browse(sid, name, path)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except backups.BackupError as e:
        raise HTTPException(404, str(e))


class RestorePaths(BaseModel):
    paths: list[str]


@app.post("/api/servers/{sid}/backups/{name}/restore-files", dependencies=moderator)
async def backup_restore_files(sid: str, name: str, body: RestorePaths, request: Request):
    _get(sid)
    request.state.detail = ", ".join(body.paths)[:400]
    try:
        return await backups.restore_paths(sid, name, body.paths)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except backups.BackupError as e:
        raise HTTPException(500, str(e))


@app.get("/api/servers/{sid}/backups/{name}", dependencies=moderator)
async def download_backup(sid: str, name: str):
    s = _get(sid)
    if name.endswith(".tar.gz"):
        p = os.path.join(backups.server_backup_dir(sid), os.path.basename(name))
        if not os.path.exists(p):
            raise HTTPException(404)
        return FileResponse(p, filename=os.path.basename(p))
    try:
        stream = await backups.export_zip(sid, name)
    except (ValueError, backups.BackupError):
        raise HTTPException(404)
    fname = f"{state.slugify(s['name']) or sid}-{name[:8]}.zip"
    return StreamingResponse(stream, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.delete("/api/servers/{sid}/backups/{name}", dependencies=admin)
async def delete_backup(sid: str, name: str):
    _get(sid)
    try:
        await backups.delete(sid, name)
    except ValueError:
        raise HTTPException(404)
    except backups.BackupError as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


# ------------------------------------------------------------------ alerts
class AlertSettings(BaseModel):
    url: str = ""
    events: dict = {}


@app.get("/api/alerts", dependencies=admin)
def get_alerts():
    return {**alerts.settings(), "names": alerts.EVENTS}


@app.put("/api/alerts", dependencies=admin)
def put_alerts(body: AlertSettings):
    try:
        return {**alerts.save(body.url, body.events), "names": alerts.EVENTS}
    except ValueError as e:
        raise HTTPException(422, str(e))


class AlertTest(BaseModel):
    url: str = ""


@app.post("/api/alerts/test", dependencies=admin)
async def test_alerts(body: AlertTest):
    try:
        await alerts.test(body.url or None)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"The webhook didn't accept the message: {e}")
    return {"ok": True}


# ------------------------------------------------------------------ modpack updates
@app.post("/api/servers/{sid}/pack/check", dependencies=moderator)
async def pack_check(sid: str):
    s = _get(sid)
    if not s.get("pack"):
        raise HTTPException(409, "This server doesn't use a modpack")
    return {"update": await packs.check(sid)}


@app.post("/api/servers/{sid}/pack/update", dependencies=moderator)
async def pack_update(sid: str):
    _get(sid)
    try:
        return await packs.update(sid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except backups.BackupError as e:
        raise HTTPException(500, f"The backup before updating failed, so nothing was changed: {e}")


@app.post("/api/servers/{sid}/pack/rollback", dependencies=moderator)
async def pack_rollback(sid: str):
    _get(sid)
    try:
        return await packs.rollback(sid)
    except ValueError as e:
        raise HTTPException(409, str(e))


# ------------------------------------------------------------------ schedule
@app.get("/api/servers/{sid}/schedule", dependencies=viewer)
def get_schedule(sid: str):
    s = _get(sid)
    return {"tasks": [dict(t, next_run=schedules.next_run(t), text=schedules.describe(t)) for t in s.get("schedule") or []],
            "tz": config.TZ}


class ScheduleBody(BaseModel):
    tasks: list[dict]


@app.put("/api/servers/{sid}/schedule", dependencies=moderator)
def put_schedule(sid: str, body: ScheduleBody):
    s = _get(sid)
    if len(body.tasks) > 30:
        raise HTTPException(422, "Up to 30 tasks per server")
    old = {t["id"]: t for t in s.get("schedule") or []}
    out = []
    for t in body.tasks:
        prev = old.get(str(t.get("id")))
        merged = {**(prev or {}), **t}
        if not prev or prev.get("when") != t.get("when"):
            merged["last_run"] = time.time()          # a new or re-timed task waits for its next time
        try:
            out.append(schedules.clean(merged))
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e))
    state.update(sid, schedule=out)
    return get_schedule(sid)


@app.post("/api/servers/{sid}/schedule/{tid}/run", dependencies=moderator)
def run_schedule_now(sid: str, tid: str, request: Request):
    s = _get(sid)
    task = next((t for t in s.get("schedule") or [] if t["id"] == tid), None)
    if not task:
        raise HTTPException(404)
    if not schedules.start_now(sid, task, request.state.user["name"]):
        raise HTTPException(409, "That task is already running")
    return {"ok": True}


# ------------------------------------------------------------------ UI
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ------------------------------------------------------------------ world map
@app.post("/api/servers/{sid}/map/render", dependencies=moderator)
def map_render_now(sid: str):
    _get(sid)
    if not mapper.ENABLED:
        raise HTTPException(409, "World maps are turned off (MAP_ENABLED=false)")
    mapper.set_status(sid, rendered=0, started=0, status="queued", full=True, note="Redrawing the whole map; starts within a minute")
    return {"ok": True}


_pos_cache: dict[str, tuple[float, list]] = {}
_DIMS = {"minecraft:overworld": "overworld", "minecraft:the_nether": "nether", "minecraft:the_end": "end"}
_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")


async def _positions(s: dict) -> list:
    """Where each online player is, read over RCON at most every 3 seconds."""
    sid = s["id"]
    hit = _pos_cache.get(sid)
    if hit and time.time() - hit[0] < 3:
        return hit[1]
    out = []
    for name in (_stats.get(sid, {}).get("players") or [])[:50]:
        try:
            pos = _NUM.findall((await servers.arcmd(s, f"data get entity {name} Pos")).split(":", 1)[-1])
            rot = _NUM.findall((await servers.arcmd(s, f"data get entity {name} Rotation")).split(":", 1)[-1])
            dim = re.search(r'"([a-z0-9_.-]+:[a-z0-9_/.-]+)"', await servers.arcmd(s, f"data get entity {name} Dimension"))
            d = dim.group(1) if dim else "minecraft:overworld"
            out.append({"name": name, "dim": _DIMS.get(d, d), "x": float(pos[0]), "y": float(pos[1]), "z": float(pos[2]),
                        "yaw": float(rot[0]) if rot else 0.0})
        except Exception:
            continue
    _pos_cache[sid] = (time.time(), out)
    return out


@app.get("/api/servers/{sid}/map", dependencies=viewer)
def map_index(sid: str):
    s = _get(sid)
    return {**mapper.public_index(sid), "status": s.get("map") or {}, "spawn": dict(zip(("x", "z"), mapper.spawn_point(sid)))}


@app.get("/api/servers/{sid}/map/players", dependencies=viewer)
async def map_players(sid: str):
    s = _get(sid)
    return await _positions(s) if s.get("phase") == "online" else []


@app.get("/map/{sid}/{dim}/{lod}/{tile}", dependencies=viewer)
def map_tile(sid: str, dim: str, lod: str, tile: str):
    _get(sid)
    m = re.fullmatch(r"(-?\d+)\.(-?\d+)\.png", tile)
    if dim not in ("overworld", "nether", "end") or lod not in ("full", "small") or not m:
        raise HTTPException(404)
    path = mapper.tile_path(sid, dim, lod, int(m.group(1)), int(m.group(2)))
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/heads/{name}.png", dependencies=viewer)
async def player_head(name: str):
    path = await heads.face(name)
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ Bedrock players
class BedrockBody(BaseModel):
    enabled: bool


@app.put("/api/servers/{sid}/bedrock", dependencies=moderator)
async def set_bedrock(sid: str, body: BedrockBody):
    _get(sid)
    try:
        return await (bedrock.enable(sid) if body.enabled else bedrock.disable(sid))
    except ValueError as e:
        raise HTTPException(409, str(e))


# ------------------------------------------------------------------ public maps (no sign-in)
class PublicMap(BaseModel):
    enabled: bool = False
    players: bool = True
    spawn: bool = True
    new_link: bool = False


@app.put("/api/servers/{sid}/public-map", dependencies=moderator)
def set_public_map(sid: str, body: PublicMap):
    s = _get(sid)
    cur = dict(s.get("public_map") or {})
    if body.new_link or not cur.get("token"):
        cur["token"] = secrets.token_urlsafe(12)
    cur.update(enabled=body.enabled, players=body.players, spawn=body.spawn)
    state.update(sid, public_map=cur)
    return cur


def _public_server(token: str) -> dict:
    """The server whose public map link this is, or 404 (also when it's turned off)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,40}", token or ""):
        raise HTTPException(404)
    for s in state.servers().values():
        pm = s.get("public_map") or {}
        if pm.get("enabled") and pm.get("token") and hmac.compare_digest(pm["token"], token):
            return s
    raise HTTPException(404)


@app.get("/m/{token}")
def public_map_page(token: str):
    _public_server(token)
    return FileResponse(os.path.join(STATIC, "public-map.html"), headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.get("/m/{token}/info")
def public_map_info(token: str):
    s = _public_server(token)
    pm = s["public_map"]
    idx = mapper.public_index(s["id"])
    return {"name": s["name"], "dims": idx.get("dims", {}), "players": pm.get("players", True),
            "spawn": dict(zip(("x", "z"), mapper.spawn_point(s["id"]))) if pm.get("spawn", True) else None,
            "online": s.get("phase") == "online", "updated": (s.get("map") or {}).get("rendered")}


@app.get("/m/{token}/players")
async def public_map_players(token: str):
    s = _public_server(token)
    if not s["public_map"].get("players", True) or s.get("phase") != "online":
        return []
    return [{k: p[k] for k in ("name", "dim", "x", "z", "yaw")} for p in await _positions(s)]


@app.get("/m/{token}/tile/{dim}/{lod}/{tile}")
def public_map_tile(token: str, dim: str, lod: str, tile: str):
    s = _public_server(token)
    m = re.fullmatch(r"(-?\d+)\.(-?\d+)\.png", tile)
    if dim not in ("overworld", "nether", "end") or lod not in ("full", "small") or not m:
        raise HTTPException(404)
    path = mapper.tile_path(s["id"], dim, lod, int(m.group(1)), int(m.group(2)))
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/m/{token}/head/{name}.png")
async def public_map_head(token: str, name: str):
    s = _public_server(token)
    if not s["public_map"].get("players", True):
        raise HTTPException(404)
    path = await heads.face(name)
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
