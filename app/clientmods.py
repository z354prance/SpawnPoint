"""Find and disable client-only mods that crash a dedicated server.

Modpacks sometimes ship mods that only work in a player's game (keybinds, HUDs,
menus). A server refuses to load them, e.g. Forge's
  "Barista (barista) has failed to load correctly ... for invalid dist DEDICATED_SERVER"
and crashes on every start. After a crash we read the log, find the offending mod
IDs, map them to jars, and rename those jars to .disabled so the next start works.
"""
import json
import logging
import os
import re
import zipfile

from . import dockerctl, state

log = logging.getLogger("spawnpoint.clientmods")

FORGE_FAIL = re.compile(r"(?P<name>[^\n\]\[]+?) \((?P<id>[a-z0-9_.\-]+)\) has failed to load correctly")
FABRIC_FAIL = re.compile(r"provided by '(?P<id>[a-z0-9_.\-]+)'")
CLIENT_HINTS = ("invalid dist DEDICATED_SERVER", "net/minecraft/client", "net.minecraft.client",
                "EnvType.CLIENT", "Environment type CLIENT", "for invalid dist")


def find_offenders(lines: list[str]) -> list[tuple[str, str]]:
    """(mod_id, display_name) for mods whose failure is tied to client-only code."""
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        window = "\n".join(lines[i:i + 6])
        if not any(h in window for h in CLIENT_HINTS):
            continue
        m = FORGE_FAIL.search(line)
        if m:
            name = m.group("name").split(": ")[-1].replace("§7", "").strip(" -:\t") or m.group("id")
            out.setdefault(m.group("id"), name)
            continue
        m = FABRIC_FAIL.search(line)
        if m:
            out.setdefault(m.group("id"), m.group("id"))
    return list(out.items())


_ids_cache: dict[tuple, set[str]] = {}


def jar_mod_ids(path: str) -> set[str]:
    """Mod IDs declared in a jar, cached by file size and time (every boot asks for all of them)."""
    try:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
    except OSError:
        return set()
    hit = _ids_cache.get(key)
    if hit is None:
        hit = _ids_cache[key] = _read_mod_ids(path)
        if len(_ids_cache) > 20000:
            _ids_cache.clear()
    return set(hit)


def _read_mod_ids(path: str) -> set[str]:
    ids: set[str] = set()
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for toml in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                if toml in names:
                    ids |= set(re.findall(r'modId\s*=\s*"([^"]+)"', z.read(toml).decode("utf-8", "replace")))
            for js, key in (("fabric.mod.json", "id"), ("quilt.mod.json", None)):
                if js in names:
                    try:
                        d = json.loads(z.read(js).decode("utf-8", "replace"))
                        if key:
                            ids.add(d.get(key, ""))
                        else:
                            ids.add(d.get("quilt_loader", {}).get("id", ""))
                    except ValueError:
                        pass
    except (OSError, zipfile.BadZipFile):
        pass
    return {i.lower() for i in ids if i}


def find_jar(mods_dir: str, mod_id: str) -> str | None:
    if not os.path.isdir(mods_dir):
        return None
    jars = [f for f in os.listdir(mods_dir) if f.endswith(".jar")]
    for f in jars:
        if mod_id.lower() in jar_mod_ids(os.path.join(mods_dir, f)):
            return f
    for f in jars:  # fallback: file name starts with the mod id
        if re.sub(r"[^a-z0-9]", "", f.lower()).startswith(re.sub(r"[^a-z0-9]", "", mod_id.lower())):
            return f
    return None


def autofix(s: dict, log_lines: list[str]) -> list[dict]:
    """Disable client-only mods found in a crash log. Returns what was disabled."""
    from . import servers
    mdir = servers.mods_dir(s)
    done = []
    for mod_id, name in find_offenders(log_lines):
        jar = find_jar(mdir, mod_id)
        if not jar:
            log.info("[%s] client-only mod %s not found in %s", s["id"], mod_id, mdir)
            continue
        os.rename(os.path.join(mdir, jar), os.path.join(mdir, jar + ".disabled"))
        done.append({"mod_id": mod_id, "name": name, "file": jar})
        log.info("[%s] disabled client-only mod %s (%s)", s["id"], name, jar)
    if done:
        s = state.get(s["id"])
        prev = s.get("auto_disabled", [])
        extra = dict(s.get("extra_env", {}))
        if s.get("kind") == "modrinth":
            # Keep a modpack update from bringing them back (partial file names).
            names = [x for x in extra.get("MODRINTH_EXCLUDE_FILES", "").split(",") if x]
            for d in done:
                stem = re.split(r"[-_]\d", d["file"], maxsplit=1)[0]
                if stem and stem not in names:
                    names.append(stem)
            extra["MODRINTH_EXCLUDE_FILES"] = ",".join(names)
        state.update(s["id"], auto_disabled=prev + done, extra_env=extra)
    return done


def crash_lines(sid: str) -> list[str]:
    return dockerctl.recent_logs(sid, 600)
