"""Export the validated desktop atlas as flash-resident strings and indexes."""

import hashlib
import json
from pathlib import Path
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tcm_herb.knowledge import display_text


def reading_sections(entry):
    """Keep source labels and narrative, grouped by the source's two sections."""
    sections = {"author_notes": [], "modern_pharmacology": []}
    for block in entry["blocks"]:
        if block["kind"] != "source_section" and block["raw_text"].strip():
            sections[block["context"]].append(display_text(block["raw_text"]))
    return tuple("\n\n".join(sections[key]) for key in sections)


def stage_atlas(directory, app):
    source = (directory / "atlas.json").read_bytes()
    report = json.loads((directory / "validation.json").read_text(encoding="utf-8"))
    if report["status"] != "passed" or hashlib.sha256(source).hexdigest() != report["artifacts"]["atlas.json"]["sha256"]:
        raise ValueError("Atlas does not match its successful validation report")
    data = json.loads(source)
    if data["schema_version"] != 1:
        raise ValueError("Unsupported atlas schema")
    pool, offsets = bytearray(b"\0"), {"": 0}

    def string_offset(value):
        if "\0" in value:
            raise ValueError("Atlas strings must not contain NUL")
        if value not in offsets:
            offsets[value] = len(pool)
            pool.extend(value.encode("utf-8") + b"\0")
        return offsets[value]

    categories = list(dict.fromkeys(entry["category"] for entry in data["entries"]))
    entry_rows = []
    notes_count = modern_count = 0
    for entry in data["entries"]:
        notes, modern = reading_sections(entry)
        notes_count += bool(notes)
        modern_count += bool(modern)
        values = (string_offset(entry["id"]), string_offset(entry["name"]),
                  string_offset(notes), string_offset(modern), categories.index(entry["category"]),
                  int(entry["content_status"] == "source_omitted"))
        entry_rows.append("    {" + ", ".join(map(str, values)) + "},")

    category_offsets = [string_offset(category) for category in categories]
    blob = app / "atlas_text.bin"
    blob.write_bytes(pool)
    (app / "atlas_blob.S").write_text(
        f'/* Text SHA256: {hashlib.sha256(pool).hexdigest()} */\n'
        '.section .rodata.home_atlas,"a",%progbits\n.balign 4\n'
        '.global home_atlas_blob\n.type home_atlas_blob,%object\nhome_atlas_blob:\n'
        f'.incbin "{blob.resolve().as_posix()}"\n.size home_atlas_blob, .-home_atlas_blob\n', encoding="ascii",
    )
    (app / "home_atlas_data.c").write_text(
        '#include "home_atlas.h"\n'
        'const home_atlas_entry_t home_atlas_entries[] = {\n' + "\n".join(entry_rows) + '\n};\n'
        'const uint32_t home_atlas_categories[] = {' + ", ".join(map(str, category_offsets)) + '};\n'
        f'const unsigned home_atlas_entry_count = {len(entry_rows)};\n'
        f'const unsigned home_atlas_category_count = {len(categories)};\n'
        f'const unsigned home_atlas_blob_size = {len(pool)};\n'
        f'const uint32_t home_atlas_blob_crc = 0x{zlib.crc32(pool):08x}U;\n', encoding="ascii",
    )
    manifest = {"source_json_sha256": hashlib.sha256(source).hexdigest(),
                "entries": len(entry_rows), "categories": len(categories),
                "entries_with_notes": notes_count, "entries_with_modern_pharmacology": modern_count,
                "string_bytes": len(pool), "string_crc32": f"{zlib.crc32(pool):08x}"}
    (app / "atlas_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return "".join(character for value in offsets for character in value if ord(character) >= 32)
