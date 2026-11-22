from app import config, hostinfo


def test_meminfo_parses_host_memory(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal:       263921756 kB\nMemFree:        1000 kB\nMemAvailable:   207618048 kB\n")
    m = hostinfo.meminfo(str(p))
    assert m == {"total_gb": 251.7, "available_gb": 198.0}


def test_sizes_per_server_and_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "servers"))
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
    (tmp_path / "servers" / "aoc" / "world").mkdir(parents=True)
    (tmp_path / "servers" / "aoc" / "world" / "r.0.0.mca").write_bytes(b"x" * 2048)
    (tmp_path / "servers" / ".spawnpoint").mkdir()
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "b.tar.gz").write_bytes(b"x" * 1024)
    hostinfo._refresh_sizes()
    assert hostinfo._sizes["per"] == {"aoc": 2048}
    assert hostinfo._sizes["backups_bytes"] == 1024
    d = hostinfo.disk()
    assert d["total_tb"] > 0 and d["minecraft_gb"] == 0.0


def test_history_keeps_last_hour():
    for i in range(hostinfo.HISTORY_LEN + 5):
        hostinfo.record("h", 5.0, 1.0, 10.0)
    assert len(hostinfo.history("h")) == hostinfo.HISTORY_LEN
    hostinfo.forget("h")
    assert hostinfo.history("h") == []


def test_disk_guard_levels(monkeypatch, tmp_path):
    import asyncio, os
    from app import alerts, config, hostinfo
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path)); monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path))
    sent = []
    monkeypatch.setattr(alerts, "send", lambda *a, **k: sent.append(a))
    free_gb = {"v": 100.0}

    class St:
        f_fsid, f_blocks, f_frsize = 1, 1, 1
        @property
        def f_bavail(self): return int(free_gb["v"] * 2**30)
    monkeypatch.setattr(os, "statvfs", lambda p: St())
    hostinfo._guard.update(level="ok")
    assert hostinfo.check_disks()["level"] == "ok" and not sent
    free_gb["v"] = 12
    assert hostinfo.check_disks()["level"] == "low" and len(sent) == 1
    hostinfo.check_disks(); assert len(sent) == 1                    # no repeat while it stays low
    free_gb["v"] = 2
    g = hostinfo.check_disks()
    assert g["level"] == "critical" and g["free_gb"] == 2.0 and len(sent) == 2
    from app import backups, mapper, state
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(state, "_state", {"servers": {}})
    state.put({"id": "x", "name": "x"})
    with __import__("pytest").raises(backups.BackupError):
        asyncio.run(backups.create("x"))
    assert mapper.due({"id": "x"}) is False
    free_gb["v"] = 100; hostinfo.check_disks()
    assert hostinfo.disk_level() == "ok"


def test_long_history_buckets_persist_and_serve(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import config, hostinfo, main, state, users
    hostinfo._long.clear(); hostinfo._open.clear(); hostinfo._hist.clear()
    t0 = (int(__import__("time").time()) // 300) * 300 - 2 * 3600
    for i in range(2 * 3600 // 10):                                   # two hours of 10-second samples
        hostinfo.record("aoc", 4.0, 8.0 + (i % 2), 50.0, 30.0 if i % 100 == 0 else 6.0, i % 3, now=t0 + i * 10)
    rows = hostinfo.long_history("aoc", 0)
    assert len(rows) == 23                                            # finished 5-minute buckets
    assert rows[0][1] == 4.0 and rows[0][2] == 30.0 and rows[0][4] == 9.0 and rows[0][6] == 2
    hostinfo._long.clear()                                            # survives a restart
    assert len(hostinfo.long_history("aoc", 0)) == 23
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json")); monkeypatch.setattr(state, "_state", {"servers": {}})
    state.put({"id": "aoc", "name": "aoc"})
    users._fails.clear(); users.ensure_bootstrap()
    c = TestClient(main.app)
    assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
    day = c.get("/api/servers/aoc/history?range=24h").json()
    week = c.get("/api/servers/aoc/history?range=7d").json()
    assert len(day) == 23 and len(week) == 4 and week[0]["p95"] == 30.0
    assert c.get("/api/servers/aoc/history?range=1y").status_code == 422
    hostinfo.forget("aoc")
    assert hostinfo.long_history("aoc", 0) == []
