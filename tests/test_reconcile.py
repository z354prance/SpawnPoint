"""aoc's real case: created as Forge 1.20.1, actually All of Create on NeoForge 1.21.1."""
import asyncio
import json
import os
import zipfile

from app import catalog, dockerctl, servers, state


def jar(path, modid, neo=True):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/neoforge.mods.toml" if neo else "META-INF/mods.toml",
                   f'modLoader="javafml"\n[[mods]]\nmodId="{modid}"\n')


def test_aoc_gets_corrected(tmp_path, monkeypatch):
    sdir = tmp_path / "aoc"
    (sdir / "libraries/net/neoforged/neoforge/21.1.209").mkdir(parents=True)
    (sdir / "libraries/net/minecraft/server/1.21.1-20240808.144430").mkdir(parents=True)
    mods = sdir / "mods"; mods.mkdir()
    jar(mods / "ferritecore-7.0.3-neoforge.jar", "ferritecore")          # shipped by the pack
    jar(mods / "modernfix-neoforge-5.27.27+mc1.21.1.jar", "modernfix")    # shipped by the pack
    jar(mods / "create-1.21.1-6.0.6.jar", "create")
    jar(mods / "Chunky-1.3.146.jar", "chunky", neo=False)                 # our wrong Forge builds
    jar(mods / "ferritecore-6.0.1-forge.jar", "ferritecore", neo=False)
    wrong = [{"project_id": f"id-{x}", "slug": x, "version_id": "old", "version": "1", "title": x,
              "filename": f} for x, f in [("chunky", "Chunky-1.3.146.jar"), ("spark", "spark-1.10.53-forge.jar"),
                                          ("ferrite-core", "ferritecore-6.0.1-forge.jar"),
                                          ("modernfix", "modernfix-forge-5.28.0+mc1.20.1.jar"),
                                          ("ng-haproxy-support", "ng_proxy_support-1.0.0-all.jar")]]
    s = {"id": "aoc", "name": "aoc", "kind": "curseforge", "loader": "forge", "mc_version": "1.20.1",
         "java_tag": "java21", "managed_mods": wrong, "helpers_managed": True, "real_ip_via": "x",
         "pregen": {"stage": "background", "percent": 0}}
    monkeypatch.setattr(state, "_state", {"servers": {"aoc": s}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))

    async def fake_find(slug, loader, ver):
        assert (loader, ver) == ("neoforge", "1.21.1")
        if slug == "ng-haproxy-support":
            return None                      # Forge-only mod: no NeoForge build
        return {"project_id": f"id-{slug}", "slug": slug, "version_id": f"neo-{slug}", "version": "2",
                "filename": f"{slug}-neoforge.jar", "title": slug}
    monkeypatch.setattr(catalog, "find_version", fake_find)

    s = asyncio.run(servers.reconcile_install(state.get("aoc")))
    assert (s["loader"], s["mc_version"], s["java_tag"]) == ("neoforge", "1.21.1", "java21")
    assert [m["slug"] for m in s["managed_mods"]] == ["chunky", "spark"]   # pack ships ferrite/modernfix
    assert all(m["version_id"].startswith("neo-") for m in s["managed_mods"])
    assert s["helpers_managed"] is False
    # helpers pass afterwards adds nothing the pack already has
    asyncio.run(servers.ensure_pack_helpers(state.get("aoc")))
    assert [m["slug"] for m in state.get("aoc")["managed_mods"]] == ["chunky", "spark"]
    # a second start with a correct record changes nothing
    before = json.dumps(state.get("aoc"), sort_keys=True)
    asyncio.run(servers.reconcile_install(state.get("aoc")))
    assert json.dumps(state.get("aoc"), sort_keys=True) == before


def test_find_java_by_cgroup(tmp_path):
    for pid, comm, cg in [(10, "sh", "0::/docker/abc123"), (11, "java", "0::/docker/other"),
                          (12, "java", "0::/system.slice/docker-abc123def.scope")]:
        d = tmp_path / str(pid); d.mkdir()
        (d / "comm").write_text(comm + "\n"); (d / "cgroup").write_text(cg + "\n")
    (tmp_path / "self").mkdir()
    assert dockerctl.find_java_by_cgroup("abc123def", str(tmp_path)) == 12
    assert dockerctl.find_java_by_cgroup("nope", str(tmp_path)) is None


def test_memory_migration(tmp_path, monkeypatch):
    """aoc had a hard 12 GB wall; on its next start it gets normal 12 / grow to 24."""
    from app import perf
    s = {"id": "aoc", "kind": "curseforge", "loader": "neoforge", "mod_count": 172,
         "memory": {"init_gb": 4, "max_gb": 12, "auto": True}}
    man = {"id": "man", "kind": "paper", "memory": {"init_gb": 2, "max_gb": 10, "auto": False}}
    monkeypatch.setattr(state, "_state", {"servers": {"aoc": s, "man": man}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    s = servers.refresh_memory_plan(state.get("aoc"))
    assert s["memory"] == {"init_gb": 4, "soft_gb": 12, "max_gb": 24, "auto": True}
    env = dockerctl.build_env({**s, "java_tag": "java21", "rcon_password": "x", "name": "aoc",
                               "pack": {"slug": "aoc"}}, 8)
    assert env["MAX_MEMORY"] == "24G" and "-XX:SoftMaxHeapSize=12g" in env["JVM_OPTS"]
    m = servers.refresh_memory_plan(state.get("man"))["memory"]   # manual: keeps 10 as normal
    assert (m["soft_gb"], m["max_gb"], m["auto"]) == (10, 20, False)


def test_fabric_26_isnt_recorded_as_its_loader_version(tmp_path, monkeypatch):
    """26.x Fabric has no intermediary mappings; the version was read from .fabric/server
    and came out as the loader's (0.20.1), so the next start asked for Minecraft 0.20.1."""
    from app import importer
    d = tmp_path / "t"
    (d / ".fabric" / "server" / "0.20.1-26.3").mkdir(parents=True)
    (d / "libraries" / "net" / "fabricmc" / "fabric-loader" / "0.20.1").mkdir(parents=True)
    (d / "versions" / "26.3").mkdir(parents=True)
    found = importer.detect(str(d))
    assert found["kind"] == "fabric" and found["loader_version"] == "0.20.1" and found["mc_version"] == "26.3"

    # the launcher's name says it too, even before the game has unpacked itself
    e = tmp_path / "e"
    (e / ".fabric").mkdir(parents=True)
    (e / "fabric-server-mc.26.3-loader.0.20.1-launcher.1.1.1.jar").write_bytes(b"x")
    assert importer.detect(str(e))["mc_version"] == "26.3"
    assert not importer.looks_like_mc("0.20.1") and importer.looks_like_mc("26.3") and importer.looks_like_mc("1.21.11")

    # a server already recorded wrongly is put right at the next start
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(d))
    monkeypatch.setattr(servers, "pack_mod_ids", lambda s: set())
    state.put({"id": "t", "name": "t", "kind": "fabric", "loader": "fabric", "mc_version": "0.20.1", "java_tag": "java25",
               "memory": {"max_gb": 4, "init_gb": 2}})
    s = asyncio.run(servers.reconcile_install(state.get("t")))
    assert s["mc_version"] == "26.3"


def test_server_folder_is_given_to_the_games_user(tmp_path, monkeypatch):
    """Files SpawnPoint or an import wrote were root's; Lithostitched couldn't create its config."""
    d = tmp_path / "g"
    (d / "config").mkdir(parents=True)
    (d / "config" / "voxy.json").write_text("{}")
    (d / "world" / "region").mkdir(parents=True)
    (d / "world" / "region" / "r.0.0.mca").write_bytes(b"x")
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(d))
    state.put({"id": "g", "name": "g", "kind": "fabric", "extra_env": {}})
    seen = {}
    monkeypatch.setattr(servers.os, "geteuid", lambda: 0)
    monkeypatch.setattr(servers.os, "lchown", lambda p, u, g: seen.__setitem__(os.path.relpath(p, d), (u, g)))
    n = servers.give_to_game("g")
    assert n == len(seen) and seen["config"] == (1000, 1000) and seen["config/voxy.json"] == (1000, 1000)
    assert "world/region/r.0.0.mca" in seen
    state.update("g", extra_env={"UID": "99", "GID": "100"})                # an image set to Unraid's nobody:users
    seen.clear(); servers.give_to_game("g")
    assert set(seen.values()) == {(99, 100)}
