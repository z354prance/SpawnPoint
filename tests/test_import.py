import asyncio
import io
import json
import os
import tarfile
import zipfile

import pytest

from app import dockerctl, importer, servers, state


def make_zip(path, files: dict, wrap=""):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(wrap + name, data)


def make_tgz(path, files: dict):
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            b = data.encode() if isinstance(data, str) else data
            ti = tarfile.TarInfo(name); ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))


def vanilla_jar(version):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("version.json", json.dumps({"id": version}))
    return buf.getvalue()


def test_extract_strips_wrapping_folder_and_mac_junk(tmp_path):
    make_zip(tmp_path / "s.zip", {"server.properties": "motd=x\n", "world/level.dat": "x",
                                  "mods/a.jar": "j"}, wrap="My Server/")
    with zipfile.ZipFile(tmp_path / "s.zip", "a") as z:
        z.writestr("__MACOSX/My Server/._server.properties", "junk")
    seen = []
    importer.extract(str(tmp_path / "s.zip"), str(tmp_path / "out"), lambda d, t: seen.append(d / t))
    assert sorted(os.listdir(tmp_path / "out")) == ["mods", "server.properties", "world"]
    assert seen[-1] == 1.0


def test_extract_rejects_path_escape(tmp_path):
    make_zip(tmp_path / "bad.zip", {"server.properties": "x", "../../etc/evil": "x"})
    with pytest.raises(importer.ImportError_):
        importer.extract(str(tmp_path / "bad.zip"), str(tmp_path / "out"))
    assert not (tmp_path / "etc").exists()


def test_extract_tar_and_not_an_archive(tmp_path):
    make_tgz(tmp_path / "s.tar.gz", {"srv/server.properties": "a=b\n", "srv/world/level.dat": "x"})
    importer.extract(str(tmp_path / "s.tar.gz"), str(tmp_path / "out"))
    assert (tmp_path / "out" / "world" / "level.dat").exists()
    (tmp_path / "junk.zip").write_text("not an archive")
    with pytest.raises(importer.ImportError_):
        importer.extract(str(tmp_path / "junk.zip"), str(tmp_path / "out2"))


def test_extract_checks_disk_space(tmp_path, monkeypatch):
    make_zip(tmp_path / "s.zip", {"world/region/r.0.0.mca": "x" * 1000})
    monkeypatch.setattr(importer.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    with pytest.raises(importer.ImportError_, match="disk space"):
        importer.extract(str(tmp_path / "s.zip"), str(tmp_path / "out"))


@pytest.mark.parametrize("files,expect", [
    ({"libraries/net/minecraftforge/forge/1.20.1-47.3.0/x": "", "libraries/net/minecraft/server/1.20.1-20230612.114412/x": ""},
     ("forge", "1.20.1", "47.3.0")),
    ({"libraries/net/neoforged/neoforge/21.1.77/x": "", "libraries/net/minecraft/server/1.21.1-20240808.144430/x": ""},
     ("neoforge", "1.21.1", "21.1.77")),
    ({"libraries/net/fabricmc/fabric-loader/0.16.9/x": "", "libraries/net/fabricmc/intermediary/1.21.1/x": "",
      "fabric-server-launch.jar": ""}, ("fabric", "1.21.1", "0.16.9")),
    ({"paper-1.21.1-130.jar": "", "config/paper-global.yml": "a: 1\n"}, ("paper", "1.21.1", "")),
    ({"purpur-1.20.4-2176.jar": ""}, ("purpur", "1.20.4", "")),
    ({"forge-1.16.5-36.2.39.jar": ""}, ("forge", "1.16.5", "36.2.39")),
])
def test_detect(tmp_path, files, expect):
    for name, data in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
    d = importer.detect(str(tmp_path))
    assert (d["kind"], d["mc_version"], d["loader_version"]) == expect


def test_detect_vanilla_from_jar(tmp_path):
    (tmp_path / "server.jar").write_bytes(vanilla_jar("1.21.4"))
    (tmp_path / "world").mkdir(); (tmp_path / "world" / "level.dat").write_text("x")
    d = importer.detect(str(tmp_path))
    assert (d["kind"], d["mc_version"], d["has_world"]) == ("vanilla", "1.21.4", True)


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "_state", {"servers": {}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / "servers" / sid))


def test_full_import_flow(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    make_zip(tmp_path / "up.zip", {
        "server.properties": "motd=Our world\nserver-port=25570\nenable-rcon=false\nwhite-list=true\n",
        "world/level.dat": "x", "whitelist.json": "[]",
        "libraries/net/minecraftforge/forge/1.20.1-47.3.0/x": "",
        "libraries/net/minecraft/server/1.20.1-20230612.114412/x": "",
        **{f"mods/m{i}.jar": "j" for i in range(70)},
    }, wrap="aoc-server/")
    prepared, ran = [], []
    async def fake_prepare(s): prepared.append(s["kind"])
    async def fake_run(sid, fresh): ran.append(state.get(sid)["pregen"]["stage"])
    monkeypatch.setattr(servers, "prepare", fake_prepare)
    from app import pregen
    monkeypatch.setattr(pregen, "run", fake_run)

    async def go():
        s = await importer.start_import(str(tmp_path / "up.zip"), "aoc2")
        await servers._tasks[s["id"]]
        return s["id"]
    sid = asyncio.run(go())
    s = state.get(sid)
    assert (s["kind"], s["mc_version"], s["loader_version"], s["java_tag"]) == ("forge", "1.20.1", "47.3.0", "java17")
    assert (s["memory"]["soft_gb"], s["memory"]["max_gb"]) == (10, 20) and s["home_groups"] == 2  # 70 mods
    props = dict(servers.read_properties(sid))
    assert props["motd"] == "Our world" and props["white-list"] == "true"      # kept
    assert props["server-port"] == "25565" and props["enable-rcon"] == "true"   # taken over
    assert props["rcon.password"] == s["rcon_password"]
    assert prepared == ["forge"] and ran == ["background"]  # existing world: open now, fill gaps later
    assert not (tmp_path / "up.zip").exists()                 # upload cleaned up
    env = dockerctl.build_env(s, 4)
    assert env["TYPE"] == "FORGE" and env["VERSION"] == "1.20.1" and env["FORGE_VERSION"] == "47.3.0"


def test_import_unknown_type_errors_clearly(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    make_zip(tmp_path / "up.zip", {"server.properties": "a=b\n", "world/level.dat": "x"})
    async def go():
        s = await importer.start_import(str(tmp_path / "up.zip"), "mystery")
        await servers._tasks[s["id"]]
        return s["id"]
    s = state.get(asyncio.run(go()))
    assert s["phase"] == "error" and "choosing the type" in s["phase_msg"]


def test_upload_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(importer, "UPLOAD_DIR", str(tmp_path / "uploads"))
    got = {}
    async def fake_start(path, name, kind, mc):
        got.update(path=path, name=name, size=os.path.getsize(path), kind=kind, mc=mc)
        s = {"id": "x", "name": name, "hostname": "x.mc.example.com", "created": 0, "phase": "importing",
             "pregen": {}, "memory": {"max_gb": 8}}
        state.put(s)
        return s
    monkeypatch.setattr(importer, "start_import", fake_start)
    main.app.dependency_overrides[main.require_auth] = lambda: {"name": "admin", "role": "admin"}
    c = TestClient(main.app)
    body = b"PK" + b"x" * 5000
    r = c.put("/api/import?filename=My%20Server.zip&kind=fabric&mc_version=1.21.1", content=body)
    assert r.status_code == 200 and got["name"] == "My Server" and got["size"] == len(body)
    assert (got["kind"], got["mc"]) == ("fabric", "1.21.1")
    assert c.put("/api/import?filename=server.rar", content=b"x").status_code == 400
    main.app.dependency_overrides.clear()


def test_hidden_folders_keep_their_dot(tmp_path):
    make_zip(tmp_path / "s.zip", {".fabric/server/1.21.1-server.jar": "x", "server.properties": "a=b\n"}, wrap="srv/")
    importer.extract(str(tmp_path / "s.zip"), str(tmp_path / "out"))
    assert (tmp_path / "out" / ".fabric").is_dir() and not (tmp_path / "out" / "fabric").exists()


def test_fresh_world_still_gets_its_spawn_generated(tmp_path):
    (tmp_path / "server.jar").write_bytes(vanilla_jar("1.21.4"))
    rd = tmp_path / "world" / "region"; rd.mkdir(parents=True); (tmp_path / "world" / "level.dat").write_text("x")
    for x in (-1, 0):
        for z in (-1, 0):
            (rd / f"r.{x}.{z}.mca").write_bytes(b"")
    d = importer.detect(str(tmp_path))
    assert d["has_world"] and not d["played_world"]
    for i in range(1, 9):
        (rd / f"r.{i}.5.mca").write_bytes(b"")
    assert importer.detect(str(tmp_path))["played_world"]
