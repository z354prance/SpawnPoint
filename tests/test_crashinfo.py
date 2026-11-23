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


def test_started_without_console(tmp_path, monkeypatch):
    mods = _server(tmp_path, monkeypatch)
    ex = crashinfo.explain("s", ['[Server thread/INFO]: Done (1.363s)! For help, type "help"',
                                 "mc-server-runner	Failed to stop using rcon-cli"], mods_dir=mods)
    assert ex["title"].startswith("The server started, but SpawnPoint couldn't reach its console")


def test_hung_shutdown_is_not_the_crash(tmp_path, monkeypatch):
    """The game stopped and saved, Java didn't exit, and the watchdog wrote a report a
    minute later. That report isn't a crash, and a plain stop isn't either."""
    from app import servers, state
    state.put({"id": "h", "name": "H", "phase": "online", "pregen": {"stage": "done"}})
    stop = ["[19:12:38] [Server thread/INFO] [minecraft/MinecraftServer]: Stopping server",
            "[19:12:38] [Server thread/INFO] [minecraft/MinecraftServer]: ThreadedAnvilChunkStorage: All dimensions are saved",
            "[19:13:41] [Server Watchdog/FATAL] [minecraft/ServerWatchdog]: A single server tick took 60000004.00 seconds (should be max 0.05)",
            "[19:13:41] [Server Watchdog/ERROR] This crash report has been saved to: /data/crash-reports/crash.txt"]
    assert servers._crash_reason("h", {"code": 1}, ["[19:10:00] Done (40s)!"] + stop) is None
    real = ["[19:12:30] [Server thread/ERROR] This crash report has been saved to: /data/crash-reports/real.txt"]
    assert servers._crash_reason("h", {"code": 1}, real + stop) == "crashed (a crash report was saved)"
    # the report picker skips the watchdog report about the hung exit
    d = tmp_path / "crash-reports"
    d.mkdir()
    (d / "real.txt").write_text('Description: Exception ticking world\n"Server thread" prio=5\n')
    import os, time
    (d / "watch.txt").write_text('Description: Watching Server\n"Server Watchdog" daemon\n')
    os.utime(d / "watch.txt", (time.time() + 5, time.time() + 5))
    from app import crashinfo
    assert crashinfo._newest_report(str(tmp_path), time.time() - 100).endswith("real.txt")


def test_watch_exit_ends_a_stuck_server(monkeypatch):
    from app import dockerctl, servers, state
    state.put({"id": "w", "name": "W", "phase": "online", "pregen": {"stage": "done"}})

    class Hub:
        seq = 2
        def since(self, n):
            return [(1, "Stopping server"), (2, "ThreadedAnvilChunkStorage: All dimensions are saved")][n:]

    class C:
        status = "running"
        killed = False
        def kill(self):
            C.killed = True
    monkeypatch.setattr(dockerctl, "hub", lambda sid: Hub())
    monkeypatch.setattr(dockerctl, "get_container", lambda sid: C())
    servers._exit_watch.pop("w", None)
    servers.watch_exit("w")
    assert not C.killed                                         # gives it a moment first
    servers._exit_watch["w"]["saved_at"] -= servers.EXIT_GRACE_S + 1
    servers.watch_exit("w")
    assert C.killed and state.get("w")["exit_hung"]



def test_case_twin_quest_files(tmp_path, monkeypatch):
    """FTB Quests loading a reward table twice (790a… and 790A… on Linux) crashes the server
    with "already exists in the tab's list"; the explanation names the files."""
    from app import crashinfo, dockerctl
    monkeypatch.setattr(dockerctl, "server_dir", lambda sid: str(tmp_path))
    d = tmp_path / "config" / "ftbquests" / "quests" / "reward_tables"
    d.mkdir(parents=True)
    (d / "790a03e65ce8ebe4.snbt").write_text("{}")
    (d / "790A03E65CE8EBE4.snbt").write_text("{}")
    log = ["java.lang.IllegalArgumentException: Itemstack 1 ftbquests:lootcrate already exists in the tab's list"]
    ex = crashinfo.explain("x", log)
    assert ex["title"] == "Two copies of the same file"
    assert "790a03e65ce8ebe4.snbt" in ex["detail"] and "790A03E65CE8EBE4.snbt" in ex["detail"]
    assert "ftbquests:lootcrate" in ex["detail"]


def test_data_pack_needs_a_missing_mod(tmp_path, monkeypatch):
    """Lithosphere without Lithostitched: the card blamed a harmless config warning
    (Voxy's AccessDeniedException) instead of the missing mod."""
    mods = _server(tmp_path, monkeypatch)
    log = ["[02:39:41] [main/ERROR]: could not save config",
           "java.nio.file.AccessDeniedException: ./config/voxyworldgenv3.json",
           "[02:39:42] [Worker-Main-1/ERROR]: Registry loading errors:",
           "java.lang.IllegalStateException: Failed to parse lithosphere:lakes_placement from pack mr_lithosphere",
           'Caused by: java.lang.IllegalStateException: Failed to parse either. First: Not a number: {"type":"x"}; Second: Unknown '
           "registry key in ResourceKey[minecraft:root / minecraft:worldgen/density_function_type]: lithostitched:fast_noise",
           "Second: Unknown registry key in ResourceKey[minecraft:root / minecraft:worldgen/density_function_type]: lithostitched:sin",
           "[02:39:42] [main/WARN]: Failed to load datapacks, can't proceed with server load. You can either fix your datapacks or reset to vanilla with --safeMode"]
    ex = crashinfo.explain("s", log, mods_dir=mods)
    assert ex["title"] == "A data pack needs a mod that isn't installed"
    assert "mr_lithosphere" in ex["detail"] and "lithostitched" in ex["detail"] and "Voxy" not in ex["detail"]
    assert ex["detail"].count("lithostitched") == 1 and "Add lithostitched" in ex["hint"]


def test_world_saved_with_a_removed_mod(tmp_path, monkeypatch):
    mods = _server(tmp_path, monkeypatch)
    log = ["[03:25:17] [main/WARN]: Missing data pack lithostitched",
           "[03:25:17] [main/WARN]: Missing data pack mr_lithosphere",
           '[03:25:18] [Worker-Main-2/ERROR]: Failed to decode value DataResult.Success[ResourceKey[minecraft:dimension / '
           'minecraft:overworld]] from ("minecraft:overworld", {generator:{biome_source:{delegate:{preset:"minecraft:overworld",'
           'type:"minecraft:multi_noise"},type:"lithostitched:injector"},settings:"minecraft:amplified",type:"minecraft:noise"},'
           'type:"minecraft:overworld"})  Optional.empty',
           "[03:25:18] [main/WARN]: Failed to load datapacks, can't proceed with server load.",
           "Caused by: java.lang.IllegalStateException: Overworld settings missing"]
    ex = crashinfo.explain("s", log, mods_dir=mods)
    assert ex["title"] == "The world was saved with a mod that's been removed"
    assert "lithostitched" in ex["detail"] and "Add lithostitched back" in ex["hint"]
