import asyncio
import json
import os
import zipfile

from app import clientmods, dockerctl, servers, state

CRASH = """Crash Report UUID: 4f5a4fb2-4988-4ddf-943f-6ad7c2a36f2d
 FML: 43.5
 Forge: net.minecraftforge:43.5.1[13:09:28] [main/ERROR] [minecraft/Main]: Failed to start the minecraft server
net.minecraftforge.fml.LoadingFailedException: Loading errors encountered: [
 Barista (barista) has failed to load correctly
§7java.lang.BootstrapMethodError: java.lang.RuntimeException: Attempted to load class com/ewyboy/barista/cleint/Keybindings for invalid dist DEDICATED_SERVER
]
 at net.minecraftforge.fml.ModLoader.waitForTransition(ModLoader.java:243) ~[fmlcore-1.19.2-43.5.1.jar%23611!/:?] {}
2026-11-21T13:09:29.360-0600 INFO mc-server-runner Done""".splitlines()


def make_jar(path, toml_id=None, fabric_id=None):
    with zipfile.ZipFile(path, "w") as z:
        if toml_id:
            z.writestr("META-INF/mods.toml", f'modLoader="javafml"\n[[mods]]\nmodId="{toml_id}"\n')
        if fabric_id:
            z.writestr("fabric.mod.json", json.dumps({"id": fabric_id}))


def test_find_offenders_from_real_log():
    assert clientmods.find_offenders(CRASH) == [("barista", "Barista")]
    # A normal load failure without client-side hints is left alone
    assert clientmods.find_offenders([" Foo (foo) has failed to load correctly", "java.lang.NullPointerException"]) == []


def test_find_jar_by_metadata(tmp_path):
    make_jar(tmp_path / "Barista-1.19.2-1.4.0.jar", toml_id="barista")
    make_jar(tmp_path / "other.jar", fabric_id="sodium")
    make_jar(tmp_path / "jei-1.19.2.jar", toml_id="jei")
    assert clientmods.find_jar(str(tmp_path), "barista") == "Barista-1.19.2-1.4.0.jar"
    assert clientmods.find_jar(str(tmp_path), "sodium") == "other.jar"
    assert clientmods.find_jar(str(tmp_path), "missing") is None


def test_boot_retries_after_disabling(tmp_path, monkeypatch):
    sdir = tmp_path / "pack"
    (sdir / "mods").mkdir(parents=True)
    make_jar(sdir / "mods" / "Barista-1.19.2-1.4.0.jar", toml_id="barista")
    make_jar(sdir / "mods" / "jei-1.19.2.jar", toml_id="jei")
    srv = {"id": "pack", "name": "pack", "kind": "curseforge", "loader": "forge", "phase": "installing",
           "first_boot": True, "extra_env": {}}
    monkeypatch.setattr(state, "_state", {"servers": {"pack": srv}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    boots = []

    async def fake_boot(s, burst=False):
        boots.append(1)

    async def fake_wait(s, timeout):
        # Crashes while Barista is enabled, starts once it's disabled.
        return not os.path.exists(sdir / "mods" / "Barista-1.19.2-1.4.0.jar")
    monkeypatch.setattr(servers, "_boot", fake_boot)
    monkeypatch.setattr(servers, "_wait_ready", fake_wait)
    monkeypatch.setattr(clientmods, "crash_lines", lambda sid: CRASH)

    assert asyncio.run(servers.boot_ready("pack", 10)) is True
    assert len(boots) == 2
    assert sorted(os.listdir(sdir / "mods")) == ["Barista-1.19.2-1.4.0.jar.disabled", "jei-1.19.2.jar"]
    assert state.get("pack")["auto_disabled"][0]["name"] == "Barista"


def test_boot_gives_up_on_other_crashes(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_state", {"servers": {"x": {"id": "x", "name": "x", "loader": "forge", "phase": "starting"}}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    async def fake_boot(s, burst=False): pass
    async def fake_wait(s, timeout): return False
    monkeypatch.setattr(servers, "_boot", fake_boot)
    monkeypatch.setattr(servers, "_wait_ready", fake_wait)
    monkeypatch.setattr(clientmods, "crash_lines", lambda sid: ["java.lang.OutOfMemoryError"])
    assert asyncio.run(servers.boot_ready("x", 10)) is False


def test_start_reruns_unfinished_install(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_state", {"servers": {"p": {
        "id": "p", "name": "p", "phase": "error", "first_boot": True, "memory": {"init_gb": 2},
        "pregen": {"stage": "pending"}}}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(servers, "_ram_check", lambda s: None)
    called = []
    async def fake_provision(sid): called.append(sid)
    monkeypatch.setattr(servers, "provision", fake_provision)
    async def run():
        await servers.start("p")
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert called == ["p"]


def test_java_suggestions():
    from app import javafix
    log = ["[init] Using Forge supplied run.sh script...",
           "Error occurred during initialization of boot layer",
           "java.lang.module.FindException: Error reading module: libraries/cpw/mods/bootstraplauncher/2.0.2/bootstraplauncher-2.0.2.jar",
           "Caused by: java.lang.module.InvalidModuleDescriptorException: Unsupported major.minor version 65.0"]
    assert javafix.suggest(log, "java17") == "java21"
    assert javafix.suggest(log, "java21") is None
    assert javafix.suggest(["java.lang.IllegalArgumentException: Unsupported class file major version 61"], "java17") == "java8"
    assert javafix.suggest(["Exception in thread main: compiled by a more recent version of the Java Runtime (class file version 69.0)"], "java21") == "java25"
    assert javafix.suggest(["OutOfMemoryError"], "java17") is None


def test_boot_switches_java(tmp_path, monkeypatch):
    srv = {"id": "j", "name": "j", "loader": "forge", "phase": "installing", "java_tag": "java17", "extra_env": {}}
    monkeypatch.setattr(state, "_state", {"servers": {"j": srv}})
    monkeypatch.setattr(state, "_path", str(tmp_path / "s.json"))
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path / sid))
    async def fake_boot(s, burst=False): pass
    async def fake_wait(s, timeout): return state.get("j")["java_tag"] == "java21"
    monkeypatch.setattr(servers, "_boot", fake_boot)
    monkeypatch.setattr(servers, "_wait_ready", fake_wait)
    monkeypatch.setattr(clientmods, "crash_lines", lambda sid: ["Unsupported major.minor version 65.0"])
    assert asyncio.run(servers.boot_ready("j", 10)) is True
    assert state.get("j")["java_tag"] == "java21"
