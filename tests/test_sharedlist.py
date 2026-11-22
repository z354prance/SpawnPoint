import asyncio
import json
import os

import pytest

from app import dockerctl, servers, sharedlist, state


@pytest.fixture
def two(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    monkeypatch.setattr(dockerctl, "status", lambda sid: "exited")
    for sid in ("a", "b"):
        os.makedirs(tmp_path / sid)
        (tmp_path / sid / "server.properties").write_text("online-mode=false\n")
        state.put({"id": sid, "name": sid, "phase": "stopped"})
    (tmp_path / "a" / "whitelist.json").write_text(json.dumps([{"uuid": "u-own", "name": "OwnOnly"}, {"uuid": "u-s", "name": "Steve"}]))
    return tmp_path


def names(tmp, sid):
    try:
        return sorted(e["name"] for e in json.load(open(tmp / sid / "whitelist.json")))
    except OSError:
        return []


def test_offline_uuid_matches_the_game():
    assert sharedlist.offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f"


def test_import_optin_exclude_and_removal(two):
    assert sharedlist.import_from("a") == 2                      # Steve and OwnOnly
    sharedlist.remove("OwnOnly")
    sharedlist.add(["Alex", "Jess"])
    assert [p["name"] for p in sharedlist.players()] == ["Alex", "Jess", "Steve"]
    asyncio.run(sharedlist.sync("b"))
    assert names(two, "b") == []                                 # b hasn't opted in
    sharedlist.set_server("b", enabled=True, exclude=["Jess"])
    asyncio.run(sharedlist.sync("b"))
    assert names(two, "b") == ["Alex", "Steve"]                  # everyone but the excluded one
    wl = json.load(open(two / "b" / "whitelist.json"))
    assert next(e for e in wl if e["name"] == "Alex")["uuid"] == sharedlist.offline_uuid("Alex")
    assert next(e for e in wl if e["name"] == "Steve")["uuid"] == "u-s"       # imported uuid reused
    sharedlist.set_server("a", enabled=True)
    asyncio.run(sharedlist.sync("a"))
    assert names(two, "a") == ["Alex", "Jess", "OwnOnly", "Steve"]
    # removing someone from the shared list only removes names the list added
    sharedlist.remove("Steve"); sharedlist.remove("Alex")
    asyncio.run(sharedlist.sync_all())
    assert names(two, "a") == ["Jess", "OwnOnly", "Steve"]       # Steve was a's own entry first: kept
    assert names(two, "b") == []
    # turning it off takes back what it added, nothing else
    sharedlist.set_server("a", enabled=False)
    asyncio.run(sharedlist.sync("a"))
    assert names(two, "a") == ["OwnOnly", "Steve"]


def test_running_servers_change_live(two, monkeypatch):
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running")
    state.update("b", phase="online")
    cmds = []

    async def arcmd(s, cmd):
        cmds.append(cmd); return ""
    monkeypatch.setattr(servers, "arcmd", arcmd)
    sharedlist.add(["Alex"])
    sharedlist.set_server("b", enabled=True)
    asyncio.run(sharedlist.sync("b"))
    assert cmds == ["whitelist add Alex"] and state.get("b")["shared_list"]["applied"] == ["alex"]


def test_names_are_checked(two):
    with pytest.raises(ValueError):
        sharedlist.add(["no spaces allowed"])
    sharedlist.add([".BedrockFriend"])                           # Floodgate names are fine
