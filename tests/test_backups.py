"""Incremental backups, run against the real restic binary."""
import asyncio
import io
import os
import shutil
import tarfile
import zipfile

import pytest

from app import backups, config, dockerctl, state

RESTIC = os.environ.get("RESTIC_BIN") or shutil.which("restic") or "/tmp/restic-dl/restic"
pytestmark = pytest.mark.skipif(not os.path.exists(RESTIC) and not shutil.which(RESTIC), reason="restic not installed")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "RESTIC", RESTIC)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "servers"))
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: os.path.join(config.DATA_DIR, sid))
    monkeypatch.setattr(dockerctl, "status", lambda sid: "exited")
    monkeypatch.setattr(state, "_path", str(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    state.put({"id": "aoc", "name": "aoc", "phase": "stopped"})
    d = tmp_path / "servers" / "aoc"
    (d / "world" / "region").mkdir(parents=True)
    (d / "cache").mkdir()
    (d / "world" / "region" / "r.0.0.mca").write_bytes(os.urandom(2_000_000))
    (d / "server.properties").write_text("motd=hi\n")
    (d / "cache" / "junk.bin").write_bytes(os.urandom(500_000))
    (d / "world" / "session.lock").write_text("x")
    return d


def run(c):
    return asyncio.run(c)


def test_incremental_backup_restore_export(world):
    first = run(backups.create("aoc", "manual"))
    assert first["added"] > 1_500_000                                  # the whole world the first time
    (world / "server.properties").write_text("motd=changed\n")
    second = run(backups.create("aoc", "scheduled"))
    assert second["added"] < 50_000                                    # only what changed the second time
    snaps = run(backups.list_all("aoc"))
    assert [b["reason"] for b in snaps] == ["scheduled", "manual"] and all(b["kind"] == "snapshot" for b in snaps)
    assert snaps[0]["time"] >= snaps[1]["time"] > 0
    assert state.get("aoc")["backup"]["status"] == "done"
    # restore the first backup: old properties come back, current files are kept aside
    (world / "server.properties").write_text("motd=broken\n")
    run(backups.restore("aoc", snaps[1]["name"]))
    assert (world / "server.properties").read_text() == "motd=hi\n"
    assert not (world / "cache").exists() and not (world / "world" / "session.lock").exists()   # excluded
    assert (world.parent / "aoc.pre-restore" / "server.properties").read_text() == "motd=broken\n"
    # export as a zip, made on the fly

    async def collect():
        gen = await backups.export_zip("aoc", snaps[0]["name"])
        return b"".join([c async for c in gen])
    z = zipfile.ZipFile(io.BytesIO(run(collect())))
    assert "server.properties" in z.namelist() and z.read("server.properties") == b"motd=changed\n"
    # delete one
    run(backups.delete("aoc", snaps[1]["name"]))
    assert len(run(backups.list_all("aoc"))) == 1


def test_retention(world):
    state.update("aoc", backup_keep={"last": 2})
    for i in range(4):
        (world / "server.properties").write_text(f"motd={i}\n")
        run(backups.create("aoc", "scheduled"))
    assert len(run(backups.list_all("aoc"))) == 2


def test_old_archives_still_listed_and_restorable(world, tmp_path):
    os.makedirs(backups.server_backup_dir("aoc"), exist_ok=True)
    arc = os.path.join(backups.server_backup_dir("aoc"), "aoc-20260101-000000.tar.gz")
    with tarfile.open(arc, "w:gz") as t:
        t.add(str(world), arcname="aoc")
    (world / "server.properties").write_text("motd=new\n")
    lst = run(backups.list_all("aoc"))
    assert lst[0]["kind"] == "archive"
    run(backups.restore("aoc", lst[0]["name"]))
    assert (world / "server.properties").read_text() == "motd=hi\n"


def test_restore_refuses_while_running(world, monkeypatch):
    monkeypatch.setattr(dockerctl, "status", lambda sid: "running")
    with pytest.raises(ValueError):
        run(backups.restore("aoc", "a" * 64))
    with pytest.raises(ValueError):
        monkeypatch.setattr(dockerctl, "status", lambda sid: "exited")
        run(backups.restore("aoc", "../../etc"))


def test_shared_store_dedups_across_servers(world, tmp_path):
    jar = os.urandom(3_000_000)
    (world / "mods").mkdir()
    (world / "mods" / "create.jar").write_bytes(jar)
    first = run(backups.create("aoc"))
    assert first["added"] > 4_000_000
    other = tmp_path / "servers" / "aoc2"
    (other / "mods").mkdir(parents=True)
    (other / "mods" / "create.jar").write_bytes(jar)                 # the same modpack on another server
    (other / "server.properties").write_text("motd=two\n")
    state.put({"id": "aoc2", "name": "aoc2", "phase": "stopped"})
    second = run(backups.create("aoc2"))
    assert second["added"] < 100_000                                 # the jar was already in the store
    assert len(run(backups.list_all("aoc"))) == 1 and len(run(backups.list_all("aoc2"))) == 1
    # one server's retention never touches another server's backups
    state.update("aoc2", backup_keep={"last": 1})
    run(backups.create("aoc2"))
    assert len(run(backups.list_all("aoc"))) == 1 and len(run(backups.list_all("aoc2"))) == 1


def test_old_per_server_store_still_works(world):
    import subprocess
    old = backups.old_repo("aoc")
    os.makedirs(old)
    env = backups._env(old)
    subprocess.run([RESTIC, "init", "-q"], env=env, check=True)
    subprocess.run([RESTIC, "backup", "-q", "--host", "aoc", "--tag", "spawnpoint", "--tag", "manual", str(world)], env=env, check=True)
    (world / "server.properties").write_text("motd=after\n")
    run(backups.create("aoc"))
    lst = run(backups.list_all("aoc"))
    assert len(lst) == 2
    older = min(lst, key=lambda b: b["time"])
    run(backups.restore("aoc", older["name"]))                       # restored from the old store
    assert (world / "server.properties").read_text() == "motd=hi\n"


def test_browse_and_restore_chosen_files(world):
    (world / "world" / "playerdata").mkdir()
    (world / "world" / "playerdata" / "steve.dat").write_text("v1")
    (world / "world" / "playerdata" / "alex.dat").write_text("a1")
    snap = run(backups.create("aoc"))["id"]
    top = run(backups.browse("aoc", snap, "/"))
    assert {e["name"] for e in top} >= {"world", "server.properties"} and all("/" not in e["name"] for e in top)
    assert [e["name"] for e in run(backups.browse("aoc", snap, "/world/playerdata"))] == ["alex.dat", "steve.dat"]
    # steve's data gets broken, alex's changes, the world moves on
    (world / "world" / "playerdata" / "steve.dat").write_text("broken")
    (world / "world" / "playerdata" / "alex.dat").write_text("a2")
    (world / "server.properties").write_text("motd=new\n")
    r = run(backups.restore_paths("aoc", snap, ["/world/playerdata/steve.dat"]))
    assert r["restored"] == 1
    assert (world / "world" / "playerdata" / "steve.dat").read_text() == "v1"
    assert (world / "world" / "playerdata" / "alex.dat").read_text() == "a2"          # untouched
    assert (world / "server.properties").read_text() == "motd=new\n"                   # untouched
    kept = world.parent / "aoc.pre-restore-files"
    assert any(p.read_text() == "broken" for p in kept.rglob("steve.dat"))
    with pytest.raises(ValueError):
        run(backups.restore_paths("aoc", snap, ["/../../etc/passwd"]))
    with pytest.raises(ValueError):
        run(backups.browse("aoc", "old.tar.gz", "/"))
