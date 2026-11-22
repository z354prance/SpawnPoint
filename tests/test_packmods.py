"""Modpack servers: extra mods go through MODRINTH_PROJECTS (the image deletes loose jars),
and the router checks which kind of connection a server actually accepts."""
import asyncio

from app import catalog, dockerctl, proxy, proxyproto, servers, state


def _setup(monkeypatch, tmp_path, srv):
    monkeypatch.setattr(state, "_state", {"servers": {srv["id"]: srv}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))


async def fake_find(project, loader, ver):
    if project == "missing":
        return None
    return {"project_id": f"id-{project}", "slug": project, "version_id": f"v-{project}", "version": "1.0",
            "filename": f"{project}-1.0.jar", "title": project.title(), "client_side": "optional"}


def test_pack_mods_go_to_managed_list(tmp_path, monkeypatch):
    srv = {"id": "aoc", "name": "aoc", "kind": "curseforge", "loader": "forge", "mc_version": "1.20.1",
           "java_tag": "java17", "rcon_password": "x", "memory": {"init_gb": 3, "max_gb": 12},
           "pack": {"slug": "all-of-create"}, "pregen": {"stage": "done", "percent": None}}
    _setup(monkeypatch, tmp_path, srv)
    monkeypatch.setattr(catalog, "find_version", fake_find)
    async def no_download(*a, **k): raise AssertionError("modpack servers must not download loose jars")
    monkeypatch.setattr(catalog, "download_mod", no_download)

    r = asyncio.run(servers.install_mod(srv, "jei"))
    assert r["managed"] and state.get("aoc")["managed_mods"][0]["slug"] == "jei"
    try:
        asyncio.run(servers.install_mod(state.get("aoc"), "missing"))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # Existing pack server: helpers are added once, and pre-gen is queued since Chunky was missing
    asyncio.run(servers.ensure_pack_helpers(state.get("aoc")))
    s = state.get("aoc")
    assert {m["slug"] for m in s["managed_mods"]} == {"jei", "chunky", "spark", "ferrite-core", "modernfix"}
    assert s["pregen"]["stage"] == "background" and s["helpers_managed"]
    env = dockerctl.build_env(s, 8)
    assert "chunky:v-chunky" in env["MODRINTH_PROJECTS"] and env["MODRINTH_LOADER"] == "forge"
    servers.remove_managed_mod("aoc", "id-jei")
    assert "jei" not in dockerctl.build_env(state.get("aoc"), 8)["MODRINTH_PROJECTS"]


def test_verify_turns_off_unaccepted_header(tmp_path, monkeypatch):
    """Like the real aoc server: the real-IP mod isn't loaded, so a PROXY header makes the
    server hang up while plain pings work. The router must switch to plain."""
    srv = {"id": "aoc", "name": "aoc", "hostname": "aoc.mc.example.com", "real_ip": True, "phase": "online"}
    _setup(monkeypatch, tmp_path, srv)

    async def run():
        async def backend(r, w):
            first = await r.read(16)
            if first.startswith(proxyproto.V2_SIG[:12]):
                w.close(); return          # doesn't understand the header: hangs up
            w.write(b"\x05\x00\x03{}}")      # any status reply
            await w.drain(); w.close()
        b = await asyncio.start_server(backend, "127.0.0.1", 0)
        port = b.sockets[0].getsockname()[1]
        monkeypatch.setattr(proxy, "backend_for", lambda s: ("127.0.0.1", port))
        await proxy.verify_real_ip("aoc")
        b.close()
    asyncio.run(run())
    s = state.get("aoc")
    assert s["real_ip"] is False and "didn't load" in s["real_ip_via"]
