"""SpawnPoint's own top-down world map.

Reads Minecraft's region files (.mca) directly and paints the top block of every column:
one pixel per block, shaded by height, with deeper water drawn darker. Each region file
(32x32 chunks, 512x512 blocks) becomes one PNG tile, plus a small 64px copy for
zoomed-out views. Works on any server type from Minecraft 1.13 on, because every server
writes the same world format.

Block colours come from the block textures: Minecraft's own (downloaded once from Mojang,
like the game launcher does) and each mod's jar for modded blocks. Without those, a
keyword table gives sensible colours, and anything unknown gets a stable made-up colour.
"""
import gzip
import hashlib
import io
import json
import os
import re
import struct
import urllib.request
import zipfile
import zlib

import numpy as np

REGION = 512
SMALL = 64                       # zoomed-out tile size

# ------------------------------------------------------------------ NBT (just what maps need)
_SKIP = {"Entities", "entities", "block_entities", "TileEntities", "structures", "Structures", "PostProcessing",
         "Lights", "fluid_ticks", "block_ticks", "TileTicks", "LiquidTicks", "CarvingMasks", "blending_data",
         "UpgradeData", "BlockLight", "SkyLight", "below_zero_retrogen", "ToBeTicked",
         "LiquidsToBeTicked", "isLightOn", "InhabitedTime", "LastUpdate", "xPos", "zPos", "yPos"}
_FIXED = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}
_FMT = {1: ">b", 2: ">h", 3: ">i", 4: ">q", 5: ">f", 6: ">d"}


def _skip(b, t, p):
    if t in _FIXED:
        return p + _FIXED[t]
    if t == 7:
        return p + 4 + struct.unpack_from(">i", b, p)[0]
    if t == 8:
        return p + 2 + struct.unpack_from(">H", b, p)[0]
    if t == 9:
        et, n = struct.unpack_from(">bi", b, p)
        p += 5
        if et in _FIXED:
            return p + n * _FIXED[et]
        for _ in range(n):
            p = _skip(b, et, p)
        return p
    if t == 10:
        while True:
            tt = b[p]
            p += 1
            if tt == 0:
                return p
            p += 2 + struct.unpack_from(">H", b, p)[0]
            p = _skip(b, tt, p)
    if t == 11:
        return p + 4 + 4 * struct.unpack_from(">i", b, p)[0]
    if t == 12:
        return p + 4 + 8 * struct.unpack_from(">i", b, p)[0]
    raise ValueError(f"bad NBT tag {t}")


def _read(b, t, p):
    if t in _FMT:
        return struct.unpack_from(_FMT[t], b, p)[0], p + _FIXED[t]
    if t == 8:
        n = struct.unpack_from(">H", b, p)[0]
        return bytes(b[p + 2:p + 2 + n]).decode("utf-8", "replace"), p + 2 + n
    if t == 10:
        out = {}
        while True:
            tt = b[p]
            p += 1
            if tt == 0:
                return out, p
            n = struct.unpack_from(">H", b, p)[0]
            name = bytes(b[p + 2:p + 2 + n]).decode("utf-8", "replace")
            p += 2 + n
            if name in _SKIP:
                p = _skip(b, tt, p)
            else:
                out[name], p = _read(b, tt, p)
    if t == 9:
        et, n = struct.unpack_from(">bi", b, p)
        p += 5
        items = []
        for _ in range(n):
            v, p = _read(b, et, p)
            items.append(v)
        return items, p
    if t == 12:
        n = struct.unpack_from(">i", b, p)[0]
        return np.frombuffer(b, dtype=">i8", count=n, offset=p + 4).astype(np.int64).view(np.uint64), p + 4 + 8 * n
    if t == 11:
        n = struct.unpack_from(">i", b, p)[0]
        return np.frombuffer(b, dtype=">i4", count=n, offset=p + 4).astype(np.int32), p + 4 + 4 * n
    if t == 7:
        n = struct.unpack_from(">i", b, p)[0]
        return bytes(b[p + 4:p + 4 + n]), p + 4 + n
    raise ValueError(f"bad NBT tag {t}")


def parse_nbt(raw: bytes) -> dict:
    t = raw[0]
    if t != 10:
        raise ValueError("not a compound")
    n = struct.unpack_from(">H", raw, 1)[0]
    return _read(raw, 10, 3 + n)[0]


def unpack(longs: np.ndarray, bits: int, count: int) -> np.ndarray:
    """Packed long array -> ints. Since 1.16 a value never spans two longs (padding at
    the top of each long); before that, values run on across longs. The array's length
    tells which layout it is (when both give the same length, they're identical)."""
    longs = np.asarray(longs, dtype=np.uint64)
    per = 64 // bits
    padded_len = -(-count // per)
    tight_len = -(-count * bits // 64)
    if len(longs) == padded_len:
        shifts = np.arange(per, dtype=np.uint64) * np.uint64(bits)
        vals = (longs[:, None] >> shifts[None, :]) & np.uint64((1 << bits) - 1)
        return vals.reshape(-1)[:count].astype(np.int64)
    if len(longs) == tight_len:
        stream = np.unpackbits(longs.astype("<u8").view(np.uint8), bitorder="little")[:count * bits]
        return stream.reshape(count, bits).astype(np.int64) @ (1 << np.arange(bits, dtype=np.int64))
    raise ValueError(f"packed array has {len(longs)} longs, expected {padded_len} or {tight_len}")


# ------------------------------------------------------------------ colours
_DYES = {"white": (233, 236, 236), "orange": (240, 118, 19), "magenta": (189, 68, 179), "light_blue": (58, 175, 217),
         "yellow": (248, 197, 39), "lime": (112, 185, 25), "pink": (237, 141, 172), "gray": (62, 68, 71),
         "light_gray": (142, 142, 134), "cyan": (21, 137, 145), "purple": (121, 42, 172), "blue": (53, 57, 157),
         "brown": (114, 71, 40), "green": (84, 109, 27), "red": (160, 39, 34), "black": (20, 21, 25)}
_KEYWORDS = [
    ("water", (63, 118, 228)), ("kelp", (63, 118, 228)), ("seagrass", (63, 118, 228)), ("bubble_column", (63, 118, 228)),
    ("lava", (207, 92, 20)), ("magma", (150, 60, 20)), ("leaves", (72, 128, 40)), ("grass_block", (110, 160, 70)),
    ("grass", (100, 150, 60)), ("fern", (90, 140, 55)), ("vine", (80, 130, 50)), ("lily_pad", (80, 140, 50)),
    ("moss", (90, 110, 45)), ("podzol", (90, 65, 30)), ("mycelium", (110, 95, 105)), ("farmland", (120, 80, 50)),
    ("dirt", (134, 96, 67)), ("mud", (80, 70, 60)), ("red_sand", (190, 103, 33)), ("sandstone", (216, 203, 155)),
    ("sand", (219, 207, 163)), ("gravel", (130, 125, 120)), ("clay", (160, 165, 180)), ("snow", (240, 245, 250)),
    ("powder_snow", (240, 245, 250)), ("blue_ice", (116, 167, 253)), ("ice", (160, 190, 240)),
    ("deepslate", (80, 80, 85)), ("tuff", (108, 109, 102)), ("calcite", (223, 224, 220)), ("andesite", (136, 136, 137)),
    ("diorite", (188, 188, 188)), ("granite", (149, 103, 85)), ("cobblestone", (122, 122, 122)), ("stone", (125, 125, 125)),
    ("ore", (125, 125, 125)), ("bedrock", (85, 85, 85)), ("terracotta", (152, 94, 67)), ("netherrack", (110, 50, 50)),
    ("nylium", (110, 40, 50)), ("soul", (80, 62, 50)), ("basalt", (80, 80, 85)), ("blackstone", (40, 36, 40)),
    ("glowstone", (171, 131, 84)), ("end_stone", (220, 222, 160)), ("purpur", (169, 125, 169)), ("obsidian", (20, 18, 30)),
    ("log", (110, 85, 50)), ("wood", (110, 85, 50)), ("stem", (90, 60, 70)), ("hyphae", (90, 60, 70)),
    ("planks", (160, 130, 80)), ("bamboo", (120, 160, 50)), ("cactus", (90, 140, 50)), ("pumpkin", (200, 120, 30)),
    ("melon", (110, 150, 40)), ("hay", (190, 160, 40)), ("brick", (150, 90, 80)), ("quartz", (235, 229, 222)),
    ("prismarine", (99, 156, 151)), ("copper", (190, 110, 80)), ("iron", (200, 200, 200)), ("gold", (230, 200, 60)),
    ("diamond", (100, 220, 220)), ("emerald", (60, 200, 100)), ("coral", (200, 90, 130)), ("glass", (200, 220, 230)),
    ("flower", (180, 160, 90)), ("tulip", (200, 90, 90)), ("rose", (200, 50, 50)), ("wheat", (190, 170, 80)),
    ("carrots", (120, 170, 60)), ("potatoes", (120, 170, 60)), ("rail", (120, 110, 100)), ("path", (148, 122, 65)),
]
_TINT = {"grass": (145, 189, 89), "foliage": (119, 171, 47), "water": (63, 118, 228)}
_AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
_WATERY = re.compile(r"(^|:)(water|bubble_column|kelp|kelp_plant|seagrass|tall_seagrass)$")


# Grass, foliage and water colours of the vanilla biomes, from the game's biome definitions.
_H = lambda v: ((v >> 16) & 255, (v >> 8) & 255, v & 255)
_SNOWY = (0x80B497, 0x60A17B)
_BIOMES = {
    "plains": (0x91BD59, 0x77AB2F), "sunflower_plains": (0x91BD59, 0x77AB2F), "beach": (0x91BD59, 0x77AB2F),
    "forest": (0x79C05A, 0x59AE30), "flower_forest": (0x79C05A, 0x59AE30),
    "birch_forest": (0x88BB67, 0x6BA941), "old_growth_birch_forest": (0x88BB67, 0x6BA941),
    "dark_forest": (0x507A32, 0x3F7A1E), "pale_garden": (0x778272, 0x878D76, 0x76889D),
    "taiga": (0x86B783, 0x68A464, 0x287082), "old_growth_pine_taiga": (0x86B783, 0x68A464, 0x287082),
    "old_growth_spruce_taiga": (0x86B783, 0x68A464, 0x287082), "snowy_taiga": (*_SNOWY, 0x3D57D6),
    "jungle": (0x59C93C, 0x30BB0B, 0x14A2C5), "bamboo_jungle": (0x59C93C, 0x30BB0B, 0x14A2C5),
    "sparse_jungle": (0x64C73F, 0x3EB80F, 0x0D8AE3),
    "swamp": (0x6A7039, 0x6A7039, 0x617B64), "mangrove_swamp": (0x6A7039, 0x8DB127, 0x3A7A6A),
    "savanna": (0xBFB755, 0xAEA42A, 0x2C8B9C), "savanna_plateau": (0xBFB755, 0xAEA42A, 0x2C8B9C),
    "windswept_savanna": (0xBFB755, 0xAEA42A, 0x2C8B9C), "desert": (0xBFB755, 0xAEA42A, 0x32A598),
    "badlands": (0x90814D, 0x9E814D, 0x4E7F81), "eroded_badlands": (0x90814D, 0x9E814D, 0x4E7F81),
    "wooded_badlands": (0x90814D, 0x9E814D, 0x4E7F81),
    "snowy_plains": (*_SNOWY, 0x3938C9), "ice_spikes": (*_SNOWY, 0x3938C9), "snowy_beach": (*_SNOWY, 0x3D57D6),
    "frozen_river": (*_SNOWY, 0x3938C9), "grove": (*_SNOWY, 0x3D57D6), "snowy_slopes": (*_SNOWY, 0x3D57D6),
    "frozen_peaks": (*_SNOWY, 0x3D57D6), "jagged_peaks": (*_SNOWY, 0x3D57D6),
    "windswept_hills": (0x8AB689, 0x6DA36B, 0x0E4ECF), "windswept_gravelly_hills": (0x8AB689, 0x6DA36B, 0x0E4ECF),
    "windswept_forest": (0x8AB689, 0x6DA36B, 0x0E4ECF), "stony_shore": (0x8AB689, 0x6DA36B, 0x0D67BB),
    "stony_peaks": (0x9ABE4B, 0x82AC1E), "meadow": (0x83BB6D, 0x63A948, 0x0E4ECF),
    "cherry_grove": (0xB6DB61, 0xB6DB61, 0x5DB7EF), "mushroom_fields": (0x55C93F, 0x2BBB0F, 0x8A8997),
    "river": (0x8EB971, 0x71A74D), "ocean": (0x8EB971, 0x71A74D), "deep_ocean": (0x8EB971, 0x71A74D),
    "warm_ocean": (0x8EB971, 0x71A74D, 0x43D5EE), "lukewarm_ocean": (0x8EB971, 0x71A74D, 0x45ADF2),
    "deep_lukewarm_ocean": (0x8EB971, 0x71A74D, 0x45ADF2), "cold_ocean": (0x8EB971, 0x71A74D, 0x3D57D6),
    "deep_cold_ocean": (0x8EB971, 0x71A74D, 0x3D57D6), "frozen_ocean": (*_SNOWY, 0x3938C9),
    "deep_frozen_ocean": (*_SNOWY, 0x3938C9), "lush_caves": (0x91BD59, 0x77AB2F),
}
# Modded biomes: guess from the name.
_BIOME_WORDS = [(re.compile(r"snow|frozen|ice|tundra|glacier|frost|alpine|arctic"), "snowy_plains"),
                (re.compile(r"taiga|boreal|conifer|pine|spruce|fir|redwood"), "taiga"),
                (re.compile(r"jungle|tropic|rainforest"), "jungle"),
                (re.compile(r"swamp|marsh|bog|bayou|fen|wetland|mire"), "swamp"),
                (re.compile(r"desert|savanna|badland|mesa|arid|dune|outback|steppe|shrub|canyon"), "savanna"),
                (re.compile(r"dark|dead|gloom|spooky|haunted"), "dark_forest"),
                (re.compile(r"birch|aspen"), "birch_forest"),
                (re.compile(r"cherry|sakura|blossom"), "cherry_grove"),
                (re.compile(r"ocean|sea|reef"), "ocean"), (re.compile(r"forest|wood|grove"), "forest")]
_biome_cache: dict[str, tuple] = {}


def biome_colors(name: str) -> tuple:
    """(grass, foliage, water) RGB for a biome id like minecraft:dark_forest."""
    hit = _biome_cache.get(name)
    if hit:
        return hit
    path = name.partition(":")[2] or name
    v = _BIOMES.get(path) if name.startswith("minecraft:") or ":" not in name else None
    if v is None:
        v = next((_BIOMES[b] for rx, b in _BIOME_WORDS if rx.search(path)), _BIOMES["plains"])
    out = (_H(v[0]), _H(v[1]), _H(v[2] if len(v) > 2 else 0x3F76E4))
    _biome_cache[name] = out
    return out


def _tint_for(path: str):
    if path in ("water", "water_still", "bubble_column"):
        return _TINT["water"]
    if path.endswith("leaves") and not any(k in path for k in ("cherry", "azalea", "pale", "spruce", "birch")):
        return _TINT["foliage"]
    if path in ("spruce_leaves",):
        return (97, 153, 97)
    if path in ("birch_leaves",):
        return (128, 167, 85)
    if path in ("grass_block", "short_grass", "grass", "tall_grass", "fern", "large_fern", "sugar_cane"):
        return _TINT["grass"]
    if path in ("vine", "lily_pad"):
        return _TINT["foliage"]
    return None


def _texture_candidates(path: str) -> list[str]:
    special = {"water": ["water_still"], "lava": ["lava_still"], "grass_block": ["grass_block_top"], "grass": ["short_grass", "grass"],
               "tall_grass": ["tall_grass_top", "short_grass"], "large_fern": ["large_fern_top", "fern"], "snow": ["snow"],
               "dirt_path": ["dirt_path_top"], "farmland": ["farmland_moist", "farmland"], "kelp": ["kelp_plant"],
               "tall_seagrass": ["tall_seagrass_top", "seagrass"], "bubble_column": ["water_still"]}
    c = list(special.get(path, [])) + [path + "_top", path]
    for suf in ("_stairs", "_slab", "_wall", "_fence_gate", "_fence", "_pressure_plate", "_button", "_wall_sign", "_sign",
                "_wall_hanging_sign", "_hanging_sign", "_pane", "_trapdoor", "_door", "_wall_torch", "_wall_fan", "_fan"):
        if path.endswith(suf):
            b = path[:-len(suf)]
            c += [b + "_top", b, b + "s", b + "_planks", b + "_block", b.replace("_brick", "_bricks"), b.replace("_tile", "_tiles")]
    if path.endswith("_carpet"):
        c.append(path[:-7] + "_wool")
    if path.endswith("_wood"):
        c.append(path[:-5] + "_log")
    if path.endswith("_hyphae"):
        c.append(path[:-7] + "_stem")
    if path.startswith("waxed_"):
        c += _texture_candidates(path[6:])
    if path.startswith("potted_"):
        c.append("flower_pot")
    return c


def jar_texture_colors(jar_path: str) -> dict:
    """{"namespace": {"texture": [r, g, b, a]}} averaged over each block texture's opaque
    pixels (the first animation frame for animated textures)."""
    from PIL import Image
    out: dict[str, dict] = {}
    rx = re.compile(r"^assets/([a-z0-9_.-]+)/textures/blocks?/([a-z0-9_/.-]+)\.png$")
    with zipfile.ZipFile(jar_path) as z:
        for name in z.namelist():
            m = rx.match(name)
            if not m:
                continue
            try:
                im = Image.open(io.BytesIO(z.read(name))).convert("RGBA")
            except Exception:
                continue
            w, h = im.size
            if h > w:                                    # animation strip: first frame
                im = im.crop((0, 0, w, w))
            a = np.asarray(im, dtype=np.float32).reshape(-1, 4)
            vis = a[a[:, 3] > 16]
            if not len(vis):
                continue
            rgb = vis[:, :3].mean(axis=0)
            alpha = a[:, 3].mean() / 255
            out.setdefault(m.group(1), {})[m.group(2)] = [int(rgb[0]), int(rgb[1]), int(rgb[2]), round(float(alpha), 2)]
    return out


class Palette:
    """Resolves block names to colours: textures first, then keywords, then a stable hash."""

    def __init__(self, textures: dict | None = None):
        self.tex = textures or {}
        self.cache: dict[str, tuple] = {}
        self.tinted: dict[str, tuple] = {}             # block -> ("grass" | "foliage" | "water", brightness)
        self.legacy_blocks: dict[int, str] = {}         # pre-1.13 modded worlds: numeric id -> name (from level.dat)
        self.legacy_biomes: dict[int, str] = {}

    def color(self, name: str) -> tuple:
        hit = self.cache.get(name)
        if hit is not None:
            return hit
        ns, _, path = name.partition(":")
        if not path:
            ns, path = "minecraft", ns
        rgb = None
        for cand in _texture_candidates(path):
            t = self.tex.get(ns, {}).get(cand)
            if t:
                rgb = tuple(t[:3])
                break
        tint = _tint_for(path) if ns == "minecraft" else ("leaves" in path and _TINT["foliage"])
        if rgb and tint:
            g = sum(rgb) / 3 / 255 if max(rgb) - min(rgb) < 25 else 1   # textures that are tinted in game are grey
            if g != 1:
                kind = next((k for k, v in _TINT.items() if v == tint), None)
                if kind:
                    self.tinted[name] = (kind, g * 1.15)        # coloured per biome when drawn
                rgb = tuple(int(c * g * 1.15) for c in tint)
        if rgb is None:
            for word, col in _DYES.items():
                if path.startswith(word + "_") and any(k in path for k in ("wool", "carpet", "concrete", "terracotta", "glass", "bed", "candle", "shulker")):
                    rgb = col
                    break
        if rgb is None:
            rgb = next((col for k, col in _KEYWORDS if k in path), None)
        if rgb is None:
            hsh = hashlib.md5(name.encode()).digest()
            rgb = (90 + hsh[0] % 110, 90 + hsh[1] % 110, 90 + hsh[2] % 110)
        rgb = tuple(int(min(255, max(0, c))) for c in rgb)
        self.cache[name] = rgb
        return rgb


# ------------------------------------------------------------------ chunk -> columns
_EARLY = {"empty", "structure_starts", "structure_references", "biomes", "noise", "surface", "carvers", "liquid_carvers",
          "minecraft:empty", "minecraft:structure_starts", "minecraft:structure_references", "minecraft:biomes",
          "minecraft:noise", "minecraft:surface", "minecraft:carvers", "minecraft:liquid_carvers"}


# Chunks the game has finished. Earlier ones sit around the edge of what's loaded and
# would show as scattered squares. ("postprocessed"/"fullchunk" are 1.13 names.)
_DONE = {"full", "postprocessed", "fullchunk"}


def _sections(level: dict):
    """{section_y: (palette_names, indices or None)} for 1.13-1.17 and 1.18+ layouts."""
    out = {}
    for sec in level.get("sections") or level.get("Sections") or []:
        y = sec.get("Y")
        if y is None:
            continue
        if "block_states" in sec:                           # 1.18+
            bs = sec["block_states"]
            pal, data = bs.get("palette") or [], bs.get("data")
        else:                                               # 1.13-1.17
            pal, data = sec.get("Palette") or [], sec.get("BlockStates")
        if not pal:
            continue
        names = [p.get("Name", "minecraft:air") if isinstance(p, dict) else "minecraft:air" for p in pal]
        if data is None or len(names) == 1:
            out[int(y)] = (names, None)
        else:
            bits = max(4, (len(names) - 1).bit_length())
            out[int(y)] = (names, (bits, data))
    return out


def _decode(entry):
    names, packed = entry
    if packed is None:
        return names, np.zeros(4096, dtype=np.int64)
    bits, data = packed
    idx = unpack(data, bits, 4096)
    return names, np.clip(idx, 0, len(names) - 1)


def _nibbles(raw: bytes | None, n: int = 4096) -> np.ndarray:
    if not raw:
        return np.zeros(n, dtype=np.int32)
    b = np.frombuffer(raw, dtype=np.uint8)
    out = np.empty(len(b) * 2, dtype=np.int32)
    out[0::2], out[1::2] = b & 15, b >> 4
    return out[:n]


def _legacy_columns(level: dict, dim: str, ids: dict | None):
    """Top block, height and water depth for a pre-1.13 chunk (numeric IDs)."""
    from . import legacy
    if not level.get("TerrainPopulated"):
        return None                                      # not finished generating
    secs = [x for x in level.get("Sections") or [] if isinstance(x, dict) and "Blocks" in x]
    if not secs:
        return None
    grid = np.zeros((256, 16, 16), dtype=np.int32)       # [y][z][x] block ids
    meta = np.zeros((256, 16, 16), dtype=np.int32)
    for sec in secs:
        y = int(sec.get("Y", 0))
        if not 0 <= y < 16:
            continue
        blocks = np.frombuffer(sec["Blocks"], dtype=np.uint8).astype(np.int32)[:4096]
        if len(blocks) < 4096:
            continue
        blocks = blocks | (_nibbles(sec.get("Add")) << 8)
        grid[y * 16:(y + 1) * 16] = blocks.reshape(16, 16, 16)
        meta[y * 16:(y + 1) * 16] = _nibbles(sec.get("Data")).reshape(16, 16, 16)
    zz, xx = np.indices((16, 16))
    solid = grid != 0
    if dim == "nether":                                  # skip the bedrock roof: highest floor below y=118 with air above
        under_air = solid[:-1] & ~solid[1:]
        under_air[118:] = False
        rev = under_air[::-1]
        has = rev.any(axis=0)
        top = 254 - rev.argmax(axis=0)
    else:
        rev = solid[::-1]
        has = rev.any(axis=0)
        top = 255 - rev.argmax(axis=0)
    heights = np.where(has, top, -1).astype(np.int32)
    tid = grid[np.clip(heights, 0, 255), zz, xx]
    tdata = meta[np.clip(heights, 0, 255), zz, xx]
    names = np.full((16, 16), "", dtype=object)
    lookup = ids or {}
    for i, d in set(zip(tid[has].tolist(), tdata[has].tolist())):
        n = lookup.get(i) or legacy.VANILLA_IDS.get(i) or f"legacy:block_{i}"
        names[has & (tid == i) & (tdata == d)] = legacy.modern_name(n, d)
    depth = np.zeros((16, 16), dtype=np.int32)
    water = has & np.isin(tid, [8, 9])
    for z, x in zip(*np.nonzero(water)):
        y = heights[z, x]
        while y > 0 and grid[y, z, x] in (8, 9) and heights[z, x] - y < 64:
            y -= 1
        depth[z, x] = heights[z, x] - y
    return names, heights, depth


def chunk_columns(nbt: dict, dim: str, ids: dict | None = None):
    """Top block name and height for each of the 16x16 columns (row = z, column = x),
    plus water depth. Returns None for chunks that aren't generated far enough to show.
    `ids` maps a modded pre-1.13 world's numeric block ids to names."""
    level = nbt.get("Level", nbt)
    if "Status" not in level and any(isinstance(x, dict) and "Blocks" in x for x in level.get("Sections") or []):
        return _legacy_columns(level, dim, ids)
    status = level.get("Status") or ""
    if status in _EARLY:
        return None
    dv = nbt.get("DataVersion", 0)
    min_y = -64 if (dv >= 2825 and dim == "overworld") else 0
    secs = _sections(level)
    if not secs:
        return None
    hm = level.get("Heightmaps") or {}
    names = np.full((16, 16), "", dtype=object)
    heights = np.full((16, 16), min_y - 1, dtype=np.int32)
    depth = np.zeros((16, 16), dtype=np.int32)
    zz, xx = np.indices((16, 16))
    if dim == "nether":
        # The Nether's heightmap is the bedrock roof. Look for the highest floor below y=120
        # that has open space above it.
        stack_names: list[str] = ["minecraft:air"]
        grid = np.zeros((128, 16, 16), dtype=np.int64)
        for sy in range(0, 8):
            if sy not in secs:
                continue
            pal, idx = _decode(secs[sy])
            base = len(stack_names)
            stack_names += pal
            grid[sy * 16:(sy + 1) * 16] = (idx + base).reshape(16, 16, 16)
        air = np.isin(np.array(stack_names, dtype=object), list(_AIR))
        is_air = air[grid]
        solid_under_air = (~is_air[:-1]) & is_air[1:]
        solid_under_air[118:] = False
        rev = solid_under_air[::-1]
        has = rev.any(axis=0)
        top = 126 - rev.argmax(axis=0)
        heights = np.where(has, top, -1).astype(np.int32)
        ids = grid[np.clip(heights, 0, 127), zz, xx]
        lut = np.array(stack_names, dtype=object)
        names = np.where(has, lut[ids], "")
        return names, heights, depth
    mb = hm.get("MOTION_BLOCKING")
    if mb is None:
        mb = hm.get("WORLD_SURFACE")
    if mb is None or len(mb) == 0:
        return None
    h = unpack(mb, 9, 256).reshape(16, 16)
    heights = (h - 1 + min_y).astype(np.int32)
    valid = h > 0
    sec_y = heights >> 4
    decoded = {}
    for sy in np.unique(sec_y[valid]):
        sy = int(sy)
        if sy in secs:
            decoded[sy] = _decode(secs[sy])
    out = np.full((16, 16), "", dtype=object)
    for sy, (pal, idx) in decoded.items():
        m = valid & (sec_y == sy)
        if not m.any():
            continue
        ly = heights[m] & 15
        sel = idx[ly * 256 + zz[m] * 16 + xx[m]]
        out[m] = np.array(pal, dtype=object)[sel]
    names = out
    of = hm.get("OCEAN_FLOOR")
    if of is not None and len(of):
        floor = (unpack(of, 9, 256).reshape(16, 16) - 1 + min_y).astype(np.int32)
        depth = np.clip(heights - floor, 0, 64)
    return names, heights, depth


# ------------------------------------------------------------------ region -> image
def _numeric_biomes(level: dict, heights: np.ndarray, names: dict | None):
    """Biomes stored as numbers (before 1.18): 256 per chunk (one per column) or 1024
    (4x4x4 cells, 1.15-1.17)."""
    from . import legacy
    b = level.get("Biomes")
    if b is None:
        return None
    arr = np.frombuffer(b, dtype=np.uint8).astype(np.int32) if isinstance(b, (bytes, bytearray)) else np.asarray(b, dtype=np.int32)
    zz, xx = np.indices((16, 16))
    if len(arr) == 256:
        ids = arr.reshape(16, 16)
    elif len(arr) == 1024:
        cy = np.clip(heights, 0, 255) >> 2
        ids = arr[np.clip(cy, 0, 63) * 16 + (zz >> 2) * 4 + (xx >> 2)]
    else:
        return None
    lookup = names or {}
    out = np.full((16, 16), "minecraft:plains", dtype=object)
    for i in np.unique(ids).tolist():
        out[ids == i] = lookup.get(i) or legacy.BIOME_IDS.get(i, "minecraft:plains")
    return out


def chunk_biomes(nbt: dict, dim: str, heights: np.ndarray, names: dict | None = None):
    """Biome id of each column at its top block, or None."""
    level = nbt.get("Level", nbt)
    if dim == "overworld" and "Biomes" in level and not level.get("sections"):
        return _numeric_biomes(level, heights, names)
    secs = level.get("sections")
    if not secs or dim != "overworld":
        return None
    min_y = -64 if nbt.get("DataVersion", 0) >= 2825 else 0
    by_y = {}
    for sec in secs:
        b = sec.get("biomes")
        if not b or sec.get("Y") is None:
            continue
        pal = [p if isinstance(p, str) else "minecraft:plains" for p in (b.get("palette") or [])]
        if not pal:
            continue
        data = b.get("data")
        idx = np.zeros(64, dtype=np.int64) if data is None or len(pal) == 1 else \
            np.clip(unpack(data, max(1, (len(pal) - 1).bit_length()), 64), 0, len(pal) - 1)
        by_y[int(sec["Y"])] = (pal, idx)
    if not by_y:
        return None
    zz, xx = np.indices((16, 16))
    hy = np.clip(heights, min_y, None)
    sy = hy >> 4
    out = np.full((16, 16), "minecraft:plains", dtype=object)
    for y in np.unique(sy):
        entry = by_y.get(int(y)) or by_y[min(by_y, key=lambda k: abs(k - int(y)))]
        pal, idx = entry
        m = sy == y
        cell = ((hy[m] & 15) >> 2) * 16 + (zz[m] >> 2) * 4 + (xx[m] >> 2)
        out[m] = np.array(pal, dtype=object)[idx[cell]]
    return out


def _speckle(x0: int, z0: int, n: int) -> np.ndarray:
    """Stable per-block brightness noise, so flat ground has a little texture."""
    z, x = np.indices((n, n), dtype=np.uint64)
    h = (x + np.uint64(x0 & 0xFFFFFFFF)) * np.uint64(0x9E3779B97F4A7C15) ^ (z + np.uint64(z0 & 0xFFFFFFFF)) * np.uint64(0xC2B2AE3D27D4EB4F)
    h ^= h >> np.uint64(29); h *= np.uint64(0xBF58476D1CE4E5B9); h ^= h >> np.uint64(32)
    return ((h >> np.uint64(40)) & np.uint64(255)).astype(np.float32) / 255.0


def _blur(a: np.ndarray, r: int) -> np.ndarray:
    """Box blur over (2r+1)^2 of an HxWxC array, edges clamped."""
    p = np.pad(a, ((r + 1, r), (r + 1, r), (0, 0)), mode="edge")
    c = p.cumsum(0).cumsum(1)
    k = 2 * r + 1
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def read_chunks(path: str):
    """(local_x, local_z, nbt) for each stored chunk of a region file."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 8192:
        return
    for i in range(1024):
        off = int.from_bytes(data[i * 4:i * 4 + 3], "big")
        if off == 0:
            continue
        start = off * 4096
        if start + 5 > len(data):
            continue
        length = int.from_bytes(data[start:start + 4], "big")
        ctype = data[start + 4]
        raw = data[start + 5:start + 4 + length]
        try:
            if ctype == 2:
                raw = zlib.decompress(raw)
            elif ctype == 1:
                raw = gzip.decompress(raw)
            elif ctype != 3:
                continue                                   # LZ4 (rare) and external chunk files aren't read
            yield i % 32, i // 32, parse_nbt(raw)
        except Exception:
            continue


_STATUS_TAG = b"\x08\x00\x06Status"


def chunk_statuses(path: str, wanted: set[tuple[int, int]]) -> dict[tuple[int, int], str]:
    """Generation status ("minecraft:full", "minecraft:features", ...) of some chunks of a
    region file, keyed by (local x, local z). Missing chunks aren't in the result. Only the
    Status tag is looked at, so this is quick enough to poll."""
    out: dict[tuple[int, int], str] = {}
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return out
    if len(data) < 8192:
        return out
    for lx, lz in wanted:
        i = lz * 32 + lx
        off = int.from_bytes(data[i * 4:i * 4 + 3], "big")
        if not off or off * 4096 + 5 > len(data):
            continue
        start = off * 4096
        length = int.from_bytes(data[start:start + 4], "big")
        ctype, raw = data[start + 4], data[start + 5:start + 4 + length]
        try:
            raw = zlib.decompress(raw) if ctype == 2 else gzip.decompress(raw) if ctype == 1 else raw if ctype == 3 else b""
        except Exception:
            continue
        j = raw.find(_STATUS_TAG)
        if j < 0:
            continue
        j += len(_STATUS_TAG)
        n = int.from_bytes(raw[j:j + 2], "big")
        out[(lx, lz)] = raw[j + 2:j + 2 + n].decode("utf-8", "replace")
    return out


def is_finished(status: str) -> bool:
    return status.rpartition(":")[2] in _DONE


def render_region(path: str, dim: str, palette: Palette):
    """RGBA image array (512x512x4) for one region file, or None if it holds nothing to draw."""
    rgb = np.zeros((REGION, REGION, 3), dtype=np.float32)
    height = np.full((REGION, REGION), -10_000, dtype=np.int32)
    depth = np.zeros((REGION, REGION), dtype=np.int32)
    alpha = np.zeros((REGION, REGION), dtype=np.uint8)
    bcol = np.zeros((REGION, REGION, 9), dtype=np.float32)      # biome grass, foliage, water colours
    bcol[:] = np.array([*_H(0x91BD59), *_H(0x77AB2F), *_H(0x3F76E4)], dtype=np.float32)
    tkind = np.zeros((REGION, REGION), dtype=np.int8)
    tbright = np.ones((REGION, REGION), dtype=np.float32)
    any_chunk = False
    chunks = []
    for lx, lz, nbt in read_chunks(path):
        try:
            got = chunk_columns(nbt, dim, palette.legacy_blocks)
        except Exception:
            continue
        if not got:
            continue
        try:
            biomes = chunk_biomes(nbt, dim, got[1], palette.legacy_biomes)
        except Exception:
            biomes = None
        chunks.append((lx, lz, got, biomes))               # keep the small arrays, not the parsed chunk
    def draw_one(lx, lz, got, biomes):
        nonlocal any_chunk
        names, h, d = got
        ys, xs = lz * 16, lx * 16
        col = np.zeros((16, 16, 3), dtype=np.float32)
        filled = names != ""
        water = np.vectorize(lambda n: bool(_WATERY.search(n)) if n else False, otypes=[bool])(names)
        kind = np.zeros((16, 16), dtype=np.int8)
        bright = np.ones((16, 16), dtype=np.float32)
        for n in set(names[filled].tolist()):
            m = names == n
            col[m] = palette.color(n)
            t = palette.tinted.get(n)
            if t:
                kind[m] = ("grass", "foliage", "water").index(t[0]) + 1
                bright[m] = 0.85 if t[0] == "water" else t[1]
        kind[water] = 3                                     # seagrass, kelp and bubbles show as water
        bright[water] = 0.85
        for b in (set(biomes.reshape(-1).tolist()) if biomes is not None else {"minecraft:plains"}):
            m = (biomes == b) if biomes is not None else np.ones((16, 16), bool)
            g, f, w = biome_colors(b)
            bcol[ys:ys + 16, xs:xs + 16][m] = np.array([*g, *f, *w], dtype=np.float32)
        tkind[ys:ys + 16, xs:xs + 16] = kind
        tbright[ys:ys + 16, xs:xs + 16] = bright
        rgb[ys:ys + 16, xs:xs + 16] = col
        height[ys:ys + 16, xs:xs + 16] = np.where(filled, h, -10_000)
        depth[ys:ys + 16, xs:xs + 16] = np.where(water, np.maximum(d, 1), 0)
        alpha[ys:ys + 16, xs:xs + 16] = np.where(filled, 255, 0)
        any_chunk = any_chunk or bool(filled.any())

    for c in chunks:                                       # one odd chunk mustn't cost the whole region
        try:
            draw_one(*c)
        except Exception:
            continue
    if not any_chunk:
        return None
    # biome colours blend into each other over a few blocks, as in game
    bcol = _blur(bcol, 4)
    for k in (1, 2, 3):
        m = tkind == k
        if m.any():
            rgb[m] = np.minimum(255, bcol[m][:, (k - 1) * 3:k * 3] * tbright[m][:, None])
    # relief, like the in-game map: brighter where the ground rises to the north-west,
    # darker where it falls, with a little per-block speckle for texture
    land = depth == 0
    north = np.vstack([height[:1], height[:-1]])
    west = np.hstack([height[:, :1], height[:, :-1]])
    slope = np.clip((height - north) + (height - west) * 0.5, -5, 5)
    slope = np.where((north < -9000) | (west < -9000), 0, slope)
    m = re.search(r"r\.(-?\d+)\.(-?\d+)\.mca$", os.path.basename(path))
    rx, rz = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    noise = _speckle(rx * REGION, rz * REGION, REGION) - 0.5
    shade = np.where(land, 1 + np.sign(slope) * np.minimum(np.abs(slope), 3) * 0.07 + noise * 0.07, 1.0)
    # water: stepped bands, lighter over shallows and darker over the deep
    band = np.minimum(depth, 30) // 3
    shade = np.where(~land, 1.08 - band * 0.05 + noise * 0.02, shade)
    img = np.clip(rgb * shade[..., None], 0, 255).astype(np.uint8)
    return np.dstack([img, alpha])


def save_tiles(img: np.ndarray, out_full: str, out_small: str) -> None:
    from PIL import Image
    os.makedirs(os.path.dirname(out_full), exist_ok=True)
    os.makedirs(os.path.dirname(out_small), exist_ok=True)
    im = Image.fromarray(img, "RGBA")
    tmp = out_full + ".tmp"
    im.save(tmp, "PNG", optimize=False, compress_level=6)
    os.replace(tmp, out_full)
    tmp = out_small + ".tmp"
    im.resize((SMALL, SMALL), Image.BOX).save(tmp, "PNG")
    os.replace(tmp, out_small)


# ------------------------------------------------------------------ textures (cached)
def vanilla_jar(version: str, cache_dir: str) -> str | None:
    """Minecraft's client jar for a version, from Mojang, for its block textures."""
    path = os.path.join(cache_dir, f"client-{version}.jar")
    if os.path.exists(path):
        return path
    try:
        with urllib.request.urlopen("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json", timeout=20) as r:
            manifest = json.load(r)
        url = next(v["url"] for v in manifest["versions"] if v["id"] == version)
        with urllib.request.urlopen(url, timeout=20) as r:
            client = json.load(r)["downloads"]["client"]["url"]
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".part"
        with urllib.request.urlopen(client, timeout=120) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def load_textures(mc_version: str | None, mods_dir: str | None, cache_dir: str) -> dict:
    """Average texture colours from vanilla and every mod jar, cached by jar."""
    os.makedirs(cache_dir, exist_ok=True)
    jars = []
    if mc_version:
        vj = vanilla_jar(mc_version, cache_dir)
        if vj:
            jars.append(vj)
    if mods_dir and os.path.isdir(mods_dir):
        jars += sorted(os.path.join(mods_dir, f) for f in os.listdir(mods_dir) if f.endswith(".jar"))
    merged: dict[str, dict] = {}
    for jar in jars:
        st = os.stat(jar)
        key = hashlib.md5(f"{os.path.basename(jar)}:{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()
        cpath = os.path.join(cache_dir, f"tex-{key}.json")
        if os.path.exists(cpath):
            with open(cpath) as f:
                cols = json.load(f)
        else:
            try:
                cols = jar_texture_colors(jar)
            except Exception:
                cols = {}
            with open(cpath, "w") as f:
                json.dump(cols, f)
        for ns, tex in cols.items():
            merged.setdefault(ns, {}).update(tex)
    return merged
