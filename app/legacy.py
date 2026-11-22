"""Worlds from before Minecraft 1.13 (like 1.12.2 modpacks): numeric block and biome IDs.

Old chunks store each block as a number plus a 4-bit "data" value (wool colour, wood
type...). Vanilla numbers are fixed (the table below); modded worlds also save which number
each mod block got, in level.dat (FML > Registries). Blocks are turned into today's names,
so the same texture colours as newer worlds apply.
"""
_V = """0 air;1 stone;2 grass;3 dirt;4 cobblestone;5 planks;6 sapling;7 bedrock;8 flowing_water;9 water;10 flowing_lava;
11 lava;12 sand;13 gravel;14 gold_ore;15 iron_ore;16 coal_ore;17 log;18 leaves;19 sponge;20 glass;21 lapis_ore;22 lapis_block;
23 dispenser;24 sandstone;25 noteblock;26 bed;27 golden_rail;28 detector_rail;29 sticky_piston;30 web;31 tallgrass;32 deadbush;
33 piston;34 piston_head;35 wool;37 yellow_flower;38 red_flower;39 brown_mushroom;40 red_mushroom;41 gold_block;42 iron_block;
43 double_stone_slab;44 stone_slab;45 brick_block;46 tnt;47 bookshelf;48 mossy_cobblestone;49 obsidian;50 torch;51 fire;
52 mob_spawner;53 oak_stairs;54 chest;55 redstone_wire;56 diamond_ore;57 diamond_block;58 crafting_table;59 wheat;60 farmland;
61 furnace;62 lit_furnace;63 standing_sign;64 wooden_door;65 ladder;66 rail;67 stone_stairs;68 wall_sign;69 lever;
70 stone_pressure_plate;71 iron_door;72 wooden_pressure_plate;73 redstone_ore;74 lit_redstone_ore;75 unlit_redstone_torch;
76 redstone_torch;77 stone_button;78 snow_layer;79 ice;80 snow;81 cactus;82 clay;83 reeds;84 jukebox;85 fence;86 pumpkin;
87 netherrack;88 soul_sand;89 glowstone;90 portal;91 lit_pumpkin;92 cake;93 unpowered_repeater;94 powered_repeater;
95 stained_glass;96 trapdoor;97 monster_egg;98 stonebrick;99 brown_mushroom_block;100 red_mushroom_block;101 iron_bars;
102 glass_pane;103 melon_block;104 pumpkin_stem;105 melon_stem;106 vine;107 fence_gate;108 brick_stairs;109 stone_brick_stairs;
110 mycelium;111 waterlily;112 nether_brick;113 nether_brick_fence;114 nether_brick_stairs;115 nether_wart;116 enchanting_table;
117 brewing_stand;118 cauldron;119 end_portal;120 end_portal_frame;121 end_stone;122 dragon_egg;123 redstone_lamp;
124 lit_redstone_lamp;125 double_wooden_slab;126 wooden_slab;127 cocoa;128 sandstone_stairs;129 emerald_ore;130 ender_chest;
131 tripwire_hook;132 tripwire;133 emerald_block;134 spruce_stairs;135 birch_stairs;136 jungle_stairs;137 command_block;
138 beacon;139 cobblestone_wall;140 flower_pot;141 carrots;142 potatoes;143 wooden_button;144 skull;145 anvil;146 trapped_chest;
147 light_weighted_pressure_plate;148 heavy_weighted_pressure_plate;149 unpowered_comparator;150 powered_comparator;
151 daylight_detector;152 redstone_block;153 quartz_ore;154 hopper;155 quartz_block;156 quartz_stairs;157 activator_rail;
158 dropper;159 stained_hardened_clay;160 stained_glass_pane;161 leaves2;162 log2;163 acacia_stairs;164 dark_oak_stairs;
165 slime;166 barrier;167 iron_trapdoor;168 prismarine;169 sea_lantern;170 hay_block;171 carpet;172 hardened_clay;173 coal_block;
174 packed_ice;175 double_plant;176 standing_banner;177 wall_banner;178 daylight_detector_inverted;179 red_sandstone;
180 red_sandstone_stairs;181 double_stone_slab2;182 stone_slab2;183 spruce_fence_gate;184 birch_fence_gate;185 jungle_fence_gate;
186 dark_oak_fence_gate;187 acacia_fence_gate;188 spruce_fence;189 birch_fence;190 jungle_fence;191 dark_oak_fence;192 acacia_fence;
193 spruce_door;194 birch_door;195 jungle_door;196 acacia_door;197 dark_oak_door;198 end_rod;199 chorus_plant;200 chorus_flower;
201 purpur_block;202 purpur_pillar;203 purpur_stairs;204 purpur_double_slab;205 purpur_slab;206 end_bricks;207 beetroots;
208 grass_path;209 end_gateway;210 repeating_command_block;211 chain_command_block;212 frosted_ice;213 magma;214 nether_wart_block;
215 red_nether_brick;216 bone_block;217 structure_void;218 observer;251 concrete;252 concrete_powder;255 structure_block"""
VANILLA_IDS = {int(i): "minecraft:" + n for i, n in (x.strip().split() for x in _V.replace("\n", "").split(";"))}

DYES = ["white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
        "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black"]
WOOD = ["oak", "spruce", "birch", "jungle", "acacia", "dark_oak"]
_RENAME = {"grass": "grass_block", "tallgrass": "short_grass", "flowing_water": "water", "flowing_lava": "lava",
           "snow_layer": "snow", "snow": "snow_block", "reeds": "sugar_cane", "waterlily": "lily_pad", "grass_path": "dirt_path",
           "yellow_flower": "dandelion", "hardened_clay": "terracotta", "brick_block": "bricks", "web": "cobweb",
           "deadbush": "dead_bush", "mob_spawner": "spawner", "lit_pumpkin": "jack_o_lantern", "melon_block": "melon",
           "nether_brick": "nether_bricks", "end_bricks": "end_stone_bricks", "magma": "magma_block", "slime": "slime_block",
           "red_nether_brick": "red_nether_bricks", "quartz_ore": "nether_quartz_ore", "stonebrick": "stone_bricks",
           "lit_redstone_ore": "redstone_ore", "lit_furnace": "furnace", "noteblock": "note_block", "fence": "oak_fence",
           "trapdoor": "oak_trapdoor", "fence_gate": "oak_fence_gate", "wooden_door": "oak_door", "double_stone_slab": "smooth_stone",
           "stone_slab": "smooth_stone_slab", "brown_mushroom_block": "brown_mushroom_block", "portal": "nether_portal",
           "standing_sign": "oak_sign", "wall_sign": "oak_wall_sign", "wooden_pressure_plate": "oak_pressure_plate",
           "wooden_button": "oak_button", "monster_egg": "infested_stone", "unlit_redstone_torch": "redstone_torch",
           "lit_redstone_lamp": "redstone_lamp", "double_wooden_slab": "oak_planks", "purpur_double_slab": "purpur_block"}


def modern_name(name: str, data: int) -> str:
    """Today's block name for an old block + data value, e.g. ("minecraft:wool", 14) -> "minecraft:red_wool"."""
    ns, _, n = name.partition(":")
    if ns != "minecraft":
        return name
    d = data & 15
    if n in ("wool", "carpet", "stained_glass", "stained_glass_pane", "concrete", "concrete_powder"):
        base = {"stained_glass": "stained_glass", "stained_glass_pane": "stained_glass_pane"}.get(n, n)
        return f"minecraft:{DYES[d]}_{base}"
    if n == "stained_hardened_clay":
        return f"minecraft:{DYES[d]}_terracotta"
    if n in ("log", "leaves"):                       # data & 3 = wood type, the rest is orientation/decay
        return f"minecraft:{WOOD[d & 3]}_{n}"
    if n in ("planks", "sapling", "wooden_slab"):
        return f"minecraft:{WOOD[d % 6]}_{'slab' if n == 'wooden_slab' else n}"
    if n in ("log2", "leaves2"):
        return f"minecraft:{WOOD[4 + (d & 1)]}_{n[:-1]}"
    if n == "stone":
        return "minecraft:" + ["stone", "granite", "polished_granite", "diorite", "polished_diorite", "andesite", "polished_andesite"][min(d, 6)]
    if n == "dirt":
        return "minecraft:" + ["dirt", "coarse_dirt", "podzol"][min(d, 2)]
    if n == "sand":
        return "minecraft:" + ("red_sand" if d == 1 else "sand")
    if n == "sandstone" or n == "red_sandstone":
        return "minecraft:" + n
    if n == "red_flower":
        return "minecraft:" + ["poppy", "blue_orchid", "allium", "azure_bluet", "red_tulip", "orange_tulip", "white_tulip",
                               "pink_tulip", "oxeye_daisy"][min(d, 8)]
    if n == "double_plant":
        return "minecraft:" + ["sunflower", "lilac", "tall_grass", "large_fern", "rose_bush", "peony"][min(d & 7, 5)]
    if n == "tallgrass":
        return "minecraft:" + ("fern" if d == 2 else "short_grass")
    if n == "prismarine":
        return "minecraft:" + ["prismarine", "prismarine_bricks", "dark_prismarine"][min(d, 2)]
    if n == "quartz_block":
        return "minecraft:quartz_block"
    return "minecraft:" + _RENAME.get(n, n)


# Numeric biome IDs (pre-1.13 worlds and 1.13–1.17 chunk biome arrays) -> today's names.
_B = """0 ocean;1 plains;2 desert;3 windswept_hills;4 forest;5 taiga;6 swamp;7 river;8 nether_wastes;9 the_end;10 frozen_ocean;
11 frozen_river;12 snowy_plains;13 snowy_plains;14 mushroom_fields;15 mushroom_fields;16 beach;17 desert;18 forest;19 taiga;
20 windswept_hills;21 jungle;22 jungle;23 sparse_jungle;24 deep_ocean;25 stony_shore;26 snowy_beach;27 birch_forest;
28 birch_forest;29 dark_forest;30 snowy_taiga;31 snowy_taiga;32 old_growth_pine_taiga;33 old_growth_pine_taiga;
34 windswept_forest;35 savanna;36 savanna_plateau;37 badlands;38 wooded_badlands;39 badlands;40 the_end;41 the_end;42 the_end;
43 the_end;44 warm_ocean;45 lukewarm_ocean;46 cold_ocean;47 warm_ocean;48 lukewarm_ocean;49 cold_ocean;50 frozen_ocean;
127 the_void;129 sunflower_plains;130 desert;131 windswept_gravelly_hills;132 flower_forest;133 taiga;134 swamp;140 ice_spikes;
149 jungle;151 sparse_jungle;155 old_growth_birch_forest;156 old_growth_birch_forest;157 dark_forest;158 snowy_taiga;
160 old_growth_spruce_taiga;161 old_growth_spruce_taiga;162 windswept_gravelly_hills;163 windswept_savanna;164 windswept_savanna;
165 eroded_badlands;166 wooded_badlands;167 badlands;168 bamboo_jungle;169 bamboo_jungle;170 soul_sand_valley;171 crimson_forest;
172 warped_forest;173 basalt_deltas"""
BIOME_IDS = {int(i): "minecraft:" + n for i, n in (x.strip().split() for x in _B.replace("\n", "").split(";"))}


def registry_from_level(level: dict) -> tuple[dict, dict]:
    """(block id -> name, biome id -> name) from a modded 1.12 level.dat (FML registries)."""
    blocks, biomes = {}, {}
    regs = ((level.get("FML") or {}).get("Registries") or {})
    for key, out in (("minecraft:blocks", blocks), ("minecraft:biomes", biomes)):
        for e in (regs.get(key) or {}).get("ids") or []:
            try:
                out[int(e["V"])] = str(e["K"])
            except (KeyError, ValueError, TypeError):
                pass
    return blocks, biomes
