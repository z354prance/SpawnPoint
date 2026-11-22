import os
import time
import zipfile

from app import crashinfo, dockerctl


def _server(tmp_path, monkeypatch, report: str | None = None, jars=()):
    d = tmp_path / "s"
    (d / "mods").mkdir(parents=True)
    for name, mid in jars:
        with zipfile.ZipFile(d / "mods" / name, "w") as z:
            z.writestr("META-INF/neoforge.mods.toml", f'[[mods]]\nmodId="{mid}"\n')
    if report:
        (d / "crash-reports").mkdir()
        (d / "crash-reports" / "crash-2026-11-22_12.00.00-server.txt").write_text(report)
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(d))
    return str(d / "mods")


def test_missing_dependency(tmp_path, monkeypatch):
    mods = _server(tmp_path, monkeypatch, jars=[("createaddition-1.21.1-1.3.jar", "createaddition")])
    log = ["[main/ERROR] Missing or unsupported mandatory dependencies:",
           "\tMod ID: 'create', Requested by: 'createaddition', Expected range: '[6.0.4,)', Actual version: '[MISSING]'"]
    ex = crashinfo.explain("s", log, mods_dir=mods)
    assert ex["title"] == "A mod is missing something it needs"
    assert "createaddition needs create [6.0.4,), which isn't installed" in ex["detail"]
    assert ex["mod"] == "createaddition" and ex["jar"] == "createaddition-1.21.1-1.3.jar"


def test_fabric_requirement(tmp_path, monkeypatch):
    mods = _server(tmp_path, monkeypatch)
    log = ["Incompatible mods found!",
           " - Mod 'Sodium Extra' (sodium-extra) 0.6.0 requires version 0.6.0 or later of mod 'sodium', which is missing!"]
    ex = crashinfo.explain("s", log, mods_dir=mods)
    assert ex["mod"] == "sodium-extra" and "Sodium Extra needs sodium" in ex["detail"]


def test_crash_report_blames_the_mod_in_the_trace(tmp_path, monkeypatch):
    report = """---- Minecraft Crash Report ----
Description: Ticking block entity

java.lang.NullPointerException: Cannot invoke "Object.hashCode()" because "key" is null
\tat java.base/java.util.HashMap.get(HashMap.java:556)
\tat TRANSFORMER/create@6.0.4/com.simibubi.create.content.kinetics.KineticBlockEntity.tick(KineticBlockEntity.java:120)
\tat TRANSFORMER/create@6.0.4/com.simibubi.create.foundation.blockEntity.SmartBlockEntity.tick(SmartBlockEntity.java:90)
\tat TRANSFORMER/minecraft@1.21.1/net.minecraft.world.level.Level.tickBlockEntities(Level.java:500)
"""
    mods = _server(tmp_path, monkeypatch, report, jars=[("create-1.21.1-6.0.4.jar", "create")])
    ex = crashinfo.explain("s", ["Exception in server tick loop"], since=time.time() - 10, mods_dir=mods)
    assert ex["title"] == "The server crashed" and ex["mod"] == "create" and ex["jar"] == "create-1.21.1-6.0.4.jar"
    assert "Ticking block entity: NullPointerException" in ex["detail"]
    assert ex["report"].startswith("crash-reports/")


def test_watchdog_names_what_the_server_was_busy_with(tmp_path, monkeypatch):
    report = """---- Minecraft Crash Report ----
// Watching Server
Description: Watching Server

java.lang.Error: Watchdog
-- Thread Dump --
"Server thread" prio=5 Id=40 RUNNABLE
\tat TRANSFORMER/minecraft@1.21.1/net.minecraft.world.level.chunk.LevelChunk.getBlockState(LevelChunk.java:200)
\tat TRANSFORMER/mekanism@10.7/mekanism.common.lib.multiblock.Structure.scan(Structure.java:88)
\tat TRANSFORMER/mekanism@10.7/mekanism.common.lib.multiblock.Structure.tick(Structure.java:40)

"Worker-Main-1" prio=5
"""
    mods = _server(tmp_path, monkeypatch, report)
    ex = crashinfo.explain("s", ["A single server tick took 60.00 seconds (should be max 0.05)"], since=time.time() - 10, mods_dir=mods)
    assert ex["title"] == "The server froze" and ex["mod"] == "mekanism"


def test_client_only_mod(tmp_path, monkeypatch):
    mods = _server(tmp_path, monkeypatch)
    log = ["java.lang.RuntimeException: Attempted to load class net/minecraft/client/gui/screens/Screen for invalid dist DEDICATED_SERVER",
           "\tat TRANSFORMER/zoomify@2.14/dev.isxander.zoomify.Zoomify.<clinit>(Zoomify.java:30)"]
    ex = crashinfo.explain("s", log, mods_dir=mods)
    assert ex["title"] == "A client-only mod is on the server" and ex["mod"] == "zoomify"


def test_mixin_failure_and_nothing_to_say(tmp_path, monkeypatch):
    mods = _server(tmp_path, monkeypatch)
    ex = crashinfo.explain("s", ["Mixin apply for mod betterclouds failed betterclouds.mixins.json:WorldRendererMixin"], mods_dir=mods)
    assert ex["title"] == "A mod failed to load" and ex["mod"] == "betterclouds"
    assert crashinfo.explain("s", ["[Server thread/INFO]: Stopping server"], mods_dir=mods)["title"] == ""
