"""Crash reports in plain words.

When a server crashes or won't start, the answer is usually one line buried in a long
crash report or log: a mod that's missing a dependency, a client-only mod, a mod that
froze the game. This reads the newest crash report and the end of the log and says,
in a sentence or two, what went wrong and which mod it points at.

explain() returns {"title", "detail", "mod", "jar", "report", "hint"}; any of the last
five can be empty. Nothing here changes the server."""
from __future__ import annotations

import glob
import os
import re
import time
from collections import Counter

from . import clientmods, dockerctl

# Code that belongs to the game, the loader or Java, never "the mod that did it"
_CORE = {"minecraft", "neoforge", "forge", "fml", "fabricloader", "fabric", "fabric-api", "java", "mixin",
         "mixinextras", "spongepowered", "quilt_loader", "quilted_fabric_api", "unknown", "jdk", "javafml",
         "lowcodefml", "mcp", "net", "com", "org", "io", "it", "sun", "kotlin", "kotlinforforge", "paper",
         "bukkit", "spigot", "c2me_base", "modernfix", "ferritecore", "connector"}

# frames like "at TRANSFORMER/create@6.0.4/com.simibubi..." (NeoForge/Forge)
_FRAME_MOD = re.compile(r"at (?:TRANSFORMER|MC-BOOTSTRAP|SECURE-BOOTSTRAP|LAYER PLUGIN|PLUGIN)?/?([a-z][a-z0-9_.\-]*)@[^/\s]+/")
# "[create-1.21.1-6.0.4.jar%23123!/:?]" at the end of a frame
_FRAME_JAR = re.compile(r"\[([A-Za-z0-9_.+\-]+\.jar)(?:%23\d+)?!")
_SUSPECT = re.compile(r"Suspected Mods?:\s*(.+)", re.I)
_MISSING_DEP = re.compile(r"Mod ID: '([^']+)', Requested by: '([^']+)', Expected range: '([^']*)', Actual version: '([^']*)'")
_FABRIC_REQ = re.compile(r"Mod '([^']+)' \(([^)]+)\) \S+ requires (?:version (.+?) of |any version of )(?:mod )?'?([^',]+)'?")
_MIXIN_FAIL = re.compile(r"Mixin apply for mod ([a-z0-9_.\-]+) failed|from mod ([a-z0-9_.\-]+) failed injection|"
                         r"Mixin \[[^\]]+\.json:[^\]]+\] from mod ([a-z0-9_.\-]+)")
_DUPLICATE = re.compile(r"(?:Found duplicate mods|Duplicate mods? found)[^\n]*", re.I)
_DUP_ID = re.compile(r"Mod ID: '([^']+)' from mod files: (.+)")


def _newest_report(base: str, since: float) -> str | None:
    d = os.path.join(base, "crash-reports")
    files = [f for f in glob.glob(os.path.join(d, "*.txt")) if os.path.getmtime(f) >= since - 60]
    return max(files, key=os.path.getmtime) if files else None


def _read(path: str, limit: int = 400_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _blame(text: str) -> str:
    """The mod that most of the top of a stack trace belongs to."""
    m = _SUSPECT.search(text)
    if m and "none" not in m.group(1).lower() and "unknown" not in m.group(1).lower():
        # "Create (create), Version: 6.0.4" -> create
        ids = re.findall(r"\(([a-z0-9_.\-]+)\)", m.group(1))
        if ids:
            return ids[0]
    votes: Counter = Counter()
    for i, line in enumerate(l for l in text.splitlines() if l.strip().startswith("at ")):
        if i > 40:                                       # the top of the trace is what matters
            break
        w = 3 if i < 8 else 1
        for mid in _FRAME_MOD.findall(line):
            if mid.lower() not in _CORE:
                votes[mid.lower()] += w
        for jar in _FRAME_JAR.findall(line):
            jid = re.split(r"[-_+]\d", jar.lower(), maxsplit=1)[0].replace(".jar", "")
            if jid and jid not in _CORE and not jid.startswith(("neoforge", "forge", "minecraft", "fabric-loader", "server")):
                votes["jar:" + jar] += w
    if not votes:
        return ""
    return votes.most_common(1)[0][0]


def _jar_for(sid: str, mods_dir: str, mod: str) -> str:
    if mod.startswith("jar:"):
        return mod[4:]
    return clientmods.find_jar(mods_dir, mod) or ""


def _exception(text: str) -> str:
    """The first exception line: "java.lang.NullPointerException: Cannot invoke ..."."""
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^(?:Caused by: )?[a-z][\w.$]+\.[A-Z]\w*(?:Exception|Error)\b", s):
            return re.sub(r"^Caused by: ", "", s)[:240]
    return ""


def _short_exc(exc: str) -> str:
    name = exc.split(":", 1)[0].rsplit(".", 1)[-1]
    msg = exc.split(":", 1)[1].strip() if ":" in exc else ""
    return f"{name}: {msg}" if msg else name


def explain(sid: str, log_lines: list[str], since: float | None = None, mods_dir: str | None = None,
            base: str | None = None) -> dict:
    """What went wrong, from the newest crash report (newer than `since`) and the log.
    `base` is the server folder (another one for an upgrade's trial copy)."""
    since = since or (time.time() - 3600)
    base = base or dockerctl.server_dir(sid)
    mods_dir = mods_dir or os.path.join(base, "mods")
    log = "\n".join(log_lines[-600:])
    out = {"title": "", "detail": "", "mod": "", "jar": "", "report": "", "hint": ""}

    def done(title, detail="", mod="", hint=""):
        out.update(title=title, detail=detail, hint=hint)
        if mod:
            out["jar"] = _jar_for(sid, mods_dir, mod)
            out["mod"] = mod[4:] if mod.startswith("jar:") else mod
        return out

    # ---- problems that stop a server from starting, from the log
    deps = _MISSING_DEP.findall(log)
    if deps:
        parts = []
        for need, by, rng, actual in deps[:4]:
            have = "isn't installed" if actual.upper() in ("[MISSING]", "MISSING", "") else f"is version {actual}"
            parts.append(f"{by} needs {need} {rng or ''}".rstrip() + f", which {have}")
        return done("A mod is missing something it needs", "; ".join(parts) + ".", deps[0][1],
                    "Add the missing mod (or the right version of it) in the Mods tab, or remove the mod that needs it.")
    fab = _FABRIC_REQ.findall(log)
    if fab or "Incompatible mods found" in log or "Incompatible mod set" in log:
        if fab:
            name, mid, ver, dep = fab[0]
            return done("A mod is missing something it needs",
                        f"{name} needs {dep}" + (f" {ver}" if ver else "") + ", which isn't installed or is the wrong version.",
                        mid, "Add or update that mod in the Mods tab, or remove the mod that needs it.")
        return done("Some mods don't work together", next((l.strip() for l in log_lines if "requires" in l or "breaks" in l), "")[:300],
                    "", "The console has the full list of which mods conflict.")
    dup = _DUP_ID.findall(log)
    if dup or _DUPLICATE.search(log):
        detail = f"Two files are the same mod ({dup[0][0]}): {dup[0][1][:200]}." if dup else _DUPLICATE.search(log).group(0)[:300]
        return done("The same mod is installed twice", detail, dup[0][0] if dup else "",
                    "Remove the older file from the Mods tab.")
    if "UnsupportedClassVersionError" in log or re.search(r"requires Java \d+", log):
        m = re.search(r"class file version (\d+)", log)
        java = {61: 17, 65: 21, 69: 25}.get(int(m.group(1)), 0) if m else 0
        return done("A mod needs a newer Java", f"Something on this server was built for Java {java}." if java else
                    "Something on this server was built for a newer Java than the server is using.", "",
                    "SpawnPoint switches Java by itself when it can; if it couldn't, the mod may not support this Minecraft version.")
    if "FAILED TO BIND TO PORT" in log.upper():
        return done("Another program is using the server's port", "The game couldn't open its port.", "",
                    "This shouldn't happen inside SpawnPoint; restart SpawnPoint's container.")

    # ---- a crash report, if one was written
    report = _newest_report(base, since)
    text = _read(report) if report else ""
    if report:
        out["report"] = os.path.relpath(report, base)
    src = text or log
    mixin = next((g for t in _MIXIN_FAIL.findall(src) for g in t if g), "")
    if mixin and mixin.lower() not in _CORE:
        return done("A mod failed to load", f"{mixin} couldn't patch the game ({_short_exc(_exception(src)) or 'mixin error'}).",
                    mixin, "This usually means that mod doesn't fit this Minecraft or loader version: update or remove it.")
    if any(h in src for h in clientmods.CLIENT_HINTS):
        mod = _blame(src)
        return done("A client-only mod is on the server", "A mod tried to load game code that only exists on players' computers.",
                    mod, "Remove it from the server; players can still use it on their side.")
    if "A single server tick took" in log or "Considering it to be crashed" in src or "Watching Server" in text:
        # the server thread's stack shows what it was stuck in
        part = text.split("Server Watchdog", 1)[-1] if "Server Watchdog" in text else src
        thread = re.search(r'"Server thread".*?(?:\n\n|\Z)', part, re.S)
        mod = _blame(thread.group(0) if thread else part)
        return done("The server froze", "One game tick ran for over a minute, so the game's watchdog stopped it." +
                    (f" The server was busy inside {mod[4:] if mod.startswith('jar:') else mod} at the time." if mod else ""),
                    mod, "Open a lag report from the Stats tab next time it slows down, or raise max-tick-time if a big "
                    "machine or world edit is expected to take long.")
    if "OutOfMemoryError" in src:
        return done("The server ran out of memory", "Java ran out of heap space.", "",
                    "Give it more memory in Settings, or check for a mod or machine that keeps loading chunks.")
    if text or "Exception" in log:
        exc = _exception(src)
        desc = re.search(r"^Description: (.+)$", text, re.M)
        mod = _blame(src)
        what = desc.group(1).strip() if desc else "The game hit an error it couldn't recover from"
        name = mod[4:] if mod.startswith("jar:") else mod
        return done("The server crashed", f"{what}" + (f": {_short_exc(exc)}" if exc else "") + "." +
                    (f" The error came from {name}." if name else ""), mod,
                    "Updating that mod usually fixes it; if it keeps happening, remove it and check the mod's issue tracker."
                    if name else "The crash report has the details.")
    return out
