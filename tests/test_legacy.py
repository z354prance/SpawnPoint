"""Maps of pre-1.13 worlds (numeric block ids, like 1.12.2 modpacks)."""
import os
import struct

import numpy as np

from app import flatmap, legacy, mapper
from tests.test_mapper import B, C, I, LISTC, S, write_region


def BA(n, data: bytes): return b"\x07" + struct.pack(">H", len(n)) + n.encode() + struct.pack(">i", len(data)) + data


def nib(vals):
    v = np.asarray(vals, dtype=np.uint8)
    return bytes((v[0::2] & 15) | ((v[1::2] & 15) << 4))


def chunk_112(populated=True, modded_id=None):
    """x < 8: grass on dirt at y=64 (plains). x >= 8: water y=58..62 over sand (ocean).
    A red wool block at (2, 64, 2); a modded block (id 600) at (3, 64, 3) if asked."""
    blocks = np.zeros((256, 16, 16), dtype=np.int32)
    data = np.zeros((256, 16, 16), dtype=np.int32)
    blocks[:60] = 1
    blocks[60:64, :, :8] = 3
    blocks[64, :, :8] = 2
    blocks[57, :, 8:] = 12
    blocks[58:63, :, 8:] = 9
    blocks[64, 2, 2], data[64, 2, 2] = 35, 14
    if modded_id:
        blocks[64, 3, 3] = modded_id
    secs = []
    for y in range(16):
        b = blocks[y * 16:(y + 1) * 16].reshape(-1)
        if not b.any():
            continue
        body = [B("Y", y), BA("Blocks", bytes((b & 255).astype(np.uint8))), BA("Data", nib(data[y * 16:(y + 1) * 16].reshape(-1)))]
        if (b > 255).any():
            body.append(BA("Add", nib(b >> 8)))
        secs.append(body)
    biomes = bytes([1 if x < 8 else 0 for z in range(16) for x in range(16)])
    level = I("xPos", 0) + I("zPos", 0) + B("TerrainPopulated", 1 if populated else 0) + LISTC("Sections", secs) + BA("Biomes", biomes)
    return C("", C("Level", level) + I("DataVersion", 1343))


def test_old_chunk_columns_and_biomes():
    names, h, depth = flatmap.chunk_columns(flatmap.parse_nbt(chunk_112()), "overworld")
    assert names[0, 0] == "minecraft:grass_block" and h[0, 0] == 64
    assert names[2, 2] == "minecraft:red_wool"
    assert names[0, 12] == "minecraft:water" and h[0, 12] == 62 and depth[0, 12] == 5
    biomes = flatmap.chunk_biomes(flatmap.parse_nbt(chunk_112()), "overworld", h)
    assert biomes[0, 0] == "minecraft:plains" and biomes[0, 12] == "minecraft:ocean"
    assert flatmap.chunk_columns(flatmap.parse_nbt(chunk_112(populated=False)), "overworld") is None


def test_modded_ids_from_level_dat():
    nbt = flatmap.parse_nbt(chunk_112(modded_id=600))
    names, _, _ = flatmap.chunk_columns(nbt, "overworld", {600: "thermalfoundation:ore"})
    assert names[3, 3] == "thermalfoundation:ore"
    names, _, _ = flatmap.chunk_columns(nbt, "overworld")
    assert names[3, 3] == "legacy:block_600"                       # unknown without the registry, still drawn
    level = {"FML": {"Registries": {"minecraft:blocks": {"ids": [{"K": "thermalfoundation:ore", "V": 600}]},
                                    "minecraft:biomes": {"ids": [{"K": "biomesoplenty:bayou", "V": 41}]}}}}
    assert legacy.registry_from_level(level) == ({600: "thermalfoundation:ore"}, {41: "biomesoplenty:bayou"})


def test_old_region_renders(tmp_path):
    path = str(tmp_path / "r.0.0.mca")
    write_region(path, {(0, 0): chunk_112(), (1, 0): chunk_112()})
    img = flatmap.render_region(path, "overworld", flatmap.Palette())
    assert img is not None and img[0, 0, 3] == 255 and img[0, 40, 3] == 0      # drawn chunks, transparent elsewhere
    land, sea = img[0, 2, :3].astype(int), img[0, 12, :3].astype(int)
    assert sea[2] > sea[0] and land[1] > land[2]                               # water is blue, grass is green


def test_texture_version_for_old_worlds():
    assert mapper.texture_version("1.12.2") == "1.16.5" and mapper.texture_version("1.7.10") == "1.16.5"
    assert mapper.texture_version("1.20.1") == "1.20.1"


def test_mid_era_biomes_1_15_to_1_17():
    arr = np.full(1024, 4, dtype=np.int32)                     # forest everywhere...
    arr[16 * 16:] = 12                                         # ...snowy plains from y=64 up (cell row 16)
    level = {"Biomes": arr}
    h = np.full((16, 16), 70); h[:, :8] = 40
    out = flatmap._numeric_biomes(level, h, None)
    assert out[0, 0] == "minecraft:forest" and out[0, 15] == "minecraft:snowy_plains"
