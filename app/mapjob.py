"""One map render, run as its own process so SpawnPoint can pause (SIGSTOP) and resume it
as a whole, and give it its own cores and a low priority.

Usage: python -m app.mapjob '<json>'  with
  {"cpus": [..], "mc_version": "1.21.1", "mods_dir": "...", "cache_dir": "...",
   "todo": [[dim, x, z, region_path, out_full, out_small], ...]}
Prints one JSON line per finished region, then {"done": true}.
"""
import json
import os
import sys
from multiprocessing import get_context

from . import flatmap

_palette = None


def _init(textures, legacy=None):
    global _palette
    try:
        os.nice(15)
    except OSError:
        pass
    _palette = flatmap.Palette(textures)
    legacy = legacy or {}
    _palette.legacy_blocks = {int(k): v for k, v in (legacy.get("blocks") or {}).items()}
    _palette.legacy_biomes = {int(k): v for k, v in (legacy.get("biomes") or {}).items()}


def _one(job):
    dim, x, z, path, out_full, out_small = job
    why = {}
    try:
        img = flatmap.render_region(path, dim, _palette, why)
        if img is None:
            for f in (out_full, out_small):
                if os.path.exists(f):
                    os.remove(f)
            return {"dim": dim, "x": x, "z": z, "ok": True, "empty": True, "skipped": why}
        flatmap.save_tiles(img, out_full, out_small)
        return {"dim": dim, "x": x, "z": z, "ok": True, "skipped": why}
    except Exception as e:
        return {"dim": dim, "x": x, "z": z, "ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


def main(arg: str) -> None:
    cfg = json.loads(arg)
    cpus = cfg.get("cpus") or []
    if cpus:
        try:
            os.sched_setaffinity(0, cpus)
        except OSError:
            pass
    try:
        os.nice(15)
    except OSError:
        pass
    textures = flatmap.load_textures(cfg.get("mc_version"), cfg.get("mods_dir"), cfg["cache_dir"])
    print(json.dumps({"textures": sum(len(v) for v in textures.values())}), flush=True)
    workers = max(1, min(len(cpus) or os.cpu_count() or 1, len(cfg["todo"]) or 1))
    with get_context("fork").Pool(workers, initializer=_init, initargs=(textures, cfg.get("legacy"))) as pool:
        for res in pool.imap_unordered(_one, cfg["todo"]):
            print(json.dumps(res), flush=True)
    print(json.dumps({"done": True}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
