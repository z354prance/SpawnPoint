import json

from app import playerhist


def test_playtime_sessions_and_where(tmp_path, monkeypatch):
    monkeypatch.setattr(playerhist.config, "STATE_DIR", str(tmp_path))
    playerhist._data.clear(); playerhist._dirty.clear()
    t = 1_000_000.0
    for i in range(7):                                  # one minute online, polled every 10 s
        playerhist.note("s", ["Steve"], t + i * 10)
    playerhist.note("s", [], t + 70)
    playerhist.note("s", ["Steve", "Alex"], t + 500)     # back later: a second visit
    for _ in range(5):
        playerhist.sample("s", [{"name": "Steve", "dim": "overworld", "x": 100.5, "z": -40.2}], t + 510)
    playerhist.sample("s", [{"name": "Steve", "dim": "nether", "x": 0, "z": 0}], t + 520)
    rows = {r["name"]: r for r in playerhist.players("s", ["Alex"])}
    assert rows["Steve"]["play_s"] == 60 and rows["Steve"]["sessions"] == 2 and not rows["Steve"]["online"]
    assert rows["Alex"]["sessions"] == 1 and rows["Alex"]["online"]
    assert rows["Steve"]["home"] == {"dim": "overworld", "x": 112, "z": -48}
    assert playerhist.players("s", ["Alex"])[0]["name"] == "Alex"       # online first
    assert playerhist.heat("s", "overworld") == {"cell": 32, "cells": [[3, -2, 5]]}
    playerhist.flush_all()
    saved = json.load(open(tmp_path / "players" / "s.json"))
    assert saved["players"]["Steve"]["sessions"] == 2
    playerhist.forget("s")
    assert not (tmp_path / "players" / "s.json").exists()
