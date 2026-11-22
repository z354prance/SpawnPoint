"""User accounts, roles and sessions.

Roles (apply to every server):
- admin:     everything, including users, the activity log and deleting servers
- moderator: run servers day to day and create/import new ones
- viewer:    see status, players and console output; can't change anything

Passwords are hashed with scrypt. Sessions are signed cookies that carry the user's
session version, so changing a password, changing a role or deleting a user logs
that user out everywhere.
"""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time

from . import config, state

ROLES = ("viewer", "moderator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
SESSION_DAYS = 30
_lock = threading.Lock()


class UserError(ValueError):
    pass


def _users() -> dict:
    return state._state.setdefault("users", {})


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def _check(password: str, stored: str) -> bool:
    try:
        _, salt, want = stored.split("$")
        got = _hash(password, bytes.fromhex(salt)).split("$")[2]
        return hmac.compare_digest(got, want)
    except ValueError:
        return False


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", name or ""))


def ensure_bootstrap() -> None:
    """First run (or an upgrade from the single-password version): create 'admin'
    with ADMIN_PASSWORD so the existing password keeps working."""
    with _lock:
        if not _users():
            _users()["admin"] = {"password": _hash(config.ADMIN_PASSWORD), "role": "admin",
                                 "created": time.time(), "session_ver": 1, "bootstrap": True}
            state.save()


def public(name: str, u: dict) -> dict:
    return {"username": name, "role": u["role"], "created": u.get("created"),
            "last_login": u.get("last_login"), "last_ip": u.get("last_ip")}


def list_users() -> list[dict]:
    return sorted((public(n, u) for n, u in _users().items()), key=lambda x: (-RANK[x["role"]], x["username"]))


def get(name: str) -> dict | None:
    return _users().get(name)


def authenticate(name: str, password: str) -> dict | None:
    u = _users().get(name)
    if u and _check(password, u["password"]):
        return u
    # spend similar time on unknown users so names can't be probed by timing
    _check(password, _hash("x"))
    return None


def note_login(name: str, ip: str) -> None:
    with _lock:
        u = _users()[name]
        u["last_login"], u["last_ip"] = time.time(), ip
        state.save()


def _admins() -> list[str]:
    return [n for n, u in _users().items() if u["role"] == "admin"]


def create(name: str, password: str, role: str) -> None:
    if not valid_name(name):
        raise UserError("Usernames are 2–32 characters: letters, numbers, . _ -")
    if role not in ROLES:
        raise UserError("Unknown role")
    if len(password) < 8:
        raise UserError("Passwords need at least 8 characters")
    with _lock:
        if name in _users():
            raise UserError("That username is taken")
        _users()[name] = {"password": _hash(password), "role": role, "created": time.time(), "session_ver": 1}
        state.save()


def update(name: str, role: str | None = None, password: str | None = None) -> None:
    with _lock:
        u = _users().get(name)
        if not u:
            raise UserError("No such user")
        if role is not None:
            if role not in ROLES:
                raise UserError("Unknown role")
            if u["role"] == "admin" and role != "admin" and len(_admins()) == 1:
                raise UserError("There must always be at least one admin")
            u["role"] = role
        if password is not None:
            if len(password) < 8:
                raise UserError("Passwords need at least 8 characters")
            u["password"] = _hash(password)
            u.pop("bootstrap", None)
        u["session_ver"] = u.get("session_ver", 1) + 1   # sign them out everywhere
        state.save()


def delete(name: str) -> None:
    with _lock:
        u = _users().get(name)
        if not u:
            raise UserError("No such user")
        if u["role"] == "admin" and len(_admins()) == 1:
            raise UserError("There must always be at least one admin")
        del _users()[name]
        state.save()


# ------------------------------------------------------------------ sessions
def _sig(payload: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(name: str) -> str:
    u = _users()[name]
    payload = f"{name}|{int(time.time())}|{u.get('session_ver', 1)}"
    return f"{payload}|{_sig(payload)}"


def check_token(tok: str) -> tuple[str, dict] | None:
    try:
        name, ts, ver, sig = tok.split("|")
    except (ValueError, AttributeError):
        return None
    payload = f"{name}|{ts}|{ver}"
    if not hmac.compare_digest(sig, _sig(payload)):
        return None
    if time.time() - int(ts) > SESSION_DAYS * 86400:
        return None
    u = _users().get(name)
    if not u or str(u.get("session_ver", 1)) != ver:
        return None
    return name, u


# ------------------------------------------------------------------ brute-force guard
# Per address (5 tries) and per username (10 tries), so changing addresses doesn't help.
_fails: dict[str, list[float]] = {}
MAX_FAILS, MAX_USER_FAILS, WINDOW_S = 5, 10, 600


def _recent(key: str) -> list[float]:
    now = time.time()
    _fails[key] = [t for t in _fails.get(key, []) if now - t < WINDOW_S]
    return _fails[key]


def blocked(ip: str, name: str = "") -> bool:
    return len(_recent("ip:" + ip)) >= MAX_FAILS or (bool(name) and len(_recent("user:" + name.lower())) >= MAX_USER_FAILS)


def note_fail(ip: str, name: str = "") -> None:
    _fails.setdefault("ip:" + ip, []).append(time.time())
    if name:
        _fails.setdefault("user:" + name.lower(), []).append(time.time())


def clear_fails(ip: str, name: str = "") -> None:
    _fails.pop("ip:" + ip, None)
    if name:
        _fails.pop("user:" + name.lower(), None)


def new_password() -> str:
    return secrets.token_urlsafe(9)
