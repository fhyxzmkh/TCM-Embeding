"""Build and inspect the offline herb catalog from the supplied PDF."""

import argparse
from bisect import bisect_right
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile


SCHEMA_VERSION = 1
TOC_LINE = re.compile(r"^\s*(.*?)\s*[.．]{4,}\s*(\d+)\s*$")
ENTRY_HEADING = re.compile(r"^(\d{3}(?:\.\d+)?)[.．、]?\s*(\S.*?)\s*$")
CATEGORY_HEADING = re.compile(r"^第[一二三四五六七八九十百]+([章节])\s*(\S.*?)\s*$")
MODERN_HEADING = "现代药理研究"
SOURCE_END = "全文完"

# Labels describe what the source says. The context separately identifies the
# author's tasting notes and the source's "现代药理研究" section.
FIELD_LABELS = {
    "口味": "taste_notes", "（柴葛）口味": "taste_notes",
    "（口味）第一口": "taste_notes", "（功效）第二口": "reported_effects",
    "性状": "appearance", "性味归经": "properties_meridians",
    "功能主治": "functions_indications", "功效": "reported_effects",
    "可以确认的功效": "reported_effects", "可以确定的功效": "reported_effects",
    "主要功效": "reported_effects", "直接功效": "reported_effects",
    "可以确认的直接功效": "reported_effects", "最核心的功效": "reported_effects",
    "功效强烈且明确": "reported_effects", "柴葛的直接功效": "reported_effects",
    "粉葛的直接功效": "reported_effects", "核桃仁的功效": "reported_effects",
    "继发功效": "secondary_effects", "间接功效": "secondary_effects",
    "衍生功效": "secondary_effects", "其他功效": "secondary_effects",
    "建议用量": "source_dosage", "嚼服剂量": "source_dosage",
    "是否毒麻药": "source_restricted_drug_flag",
    "化学成分": "chemical_components", "药理": "pharmacology",
    "毒理": "toxicology", "药代动力学": "pharmacokinetics",
    "副作用": "adverse_effect_notes", "注意": "cautions",
    "提示": "notes", "注": "notes", "PS": "notes", "Attention": "cautions",
    "使用建议": "usage_notes", "备注": "notes", "补丁": "notes", "补一条": "notes",
    "缺点": "limitations", "缺陷": "limitations",
}
FIELD_HEADING = re.compile(
    r"^(" + "|".join(re.escape(label) for label in sorted(FIELD_LABELS, key=len, reverse=True)) + r")[：:]"
)


def normalized_name(value):
    return re.sub(r"\s+", "", value)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def extract_pages(pdf, executable):
    result = subprocess.run(
        [str(executable), "-layout", "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True, check=True,
    )
    text = result.stdout.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    chunks = text.split("\f")
    if chunks[-1] == "":
        chunks.pop()
    pages = []
    for number, layout in enumerate(chunks, 1):
        lines = layout.splitlines()
        nonempty = [i for i, line in enumerate(lines) if line.strip()]
        footer = nonempty[-1] if nonempty and re.fullmatch(r"\d+", lines[nonempty[-1]].strip()) else None
        removed, content = [], []
        for index, line in enumerate(lines):
            header = number > 1 and index < 5 and re.sub(r"[\s，,]", "", line) == "好中醫親試藥"
            if index == footer or header:
                removed.append({"line": index + 1, "text": line, "reason": "footer" if index == footer else "header"})
            else:
                content.append({"pdf_page": number, "source_line": index + 1, "text": line.strip()})
        pages.append({
            "pdf_page": number,
            "printed_page": int(lines[footer].strip()) if footer is not None else None,
            "layout_text": layout,
            "removed_layout_lines": removed,
            "content_lines": content,
        })
    return pages, digest(text.encode("utf-8"))


def read_catalog(pages):
    catalog, categories, toc_pages = [], [], []
    chapter, section = None, None
    for page in pages:
        page_has_entries = False
        for line in page["content_lines"]:
            match = TOC_LINE.fullmatch(line["text"])
            if not match:
                continue
            title, book_page = match[1], int(match[2])
            category = CATEGORY_HEADING.fullmatch(title)
            if category:
                if category[1] == "章":
                    chapter, section = category[2], None
                else:
                    section = category[2]
                categories.append({"heading": title, "category": chapter, "section": section,
                                   "printed_page": book_page, "toc_pdf_page": page["pdf_page"]})
                continue
            entry = ENTRY_HEADING.fullmatch(title)
            if entry:
                if chapter is None:
                    raise ValueError("Catalog entry has no chapter")
                catalog.append({
                    "id": entry[1], "name": entry[2], "display_order": len(catalog) + 1,
                    "category": chapter, "section": section, "toc_pdf_page": page["pdf_page"],
                    "printed_page_start": book_page,
                })
                page_has_entries = True
        if page_has_entries:
            toc_pages.append(page["pdf_page"])
    ids = [entry["id"] for entry in catalog]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Catalog is empty or contains duplicate source IDs")
    return catalog, categories, toc_pages


def trimmed_lines(lines):
    start, end = 0, len(lines)
    while start < end and not lines[start]["text"]:
        start += 1
    while end > start and not lines[end - 1]["text"]:
        end -= 1
    return lines[start:end]


def source_spans(lines):
    spans = []
    for line in lines:
        if not spans or spans[-1]["pdf_page"] != line["pdf_page"]:
            spans.append({"pdf_page": line["pdf_page"], "line_start": line["source_line"],
                          "line_end": line["source_line"]})
        else:
            spans[-1]["line_end"] = line["source_line"]
    return spans


def display_text(raw):
    """Join PDF line wraps, keeping paragraph breaks and numbered subpoints."""
    paragraphs, current = [], ""
    for line in raw.splitlines():
        line = line.strip()
        numbered = re.match(r"^(?:\d+[、．.]|[（(]\d+[）)])", line)
        if not line or numbered:
            if current:
                paragraphs.append(current)
                current = ""
            if not line:
                continue
        if current and current[-1].isascii() and line[0].isascii() and current[-1].isalnum() and line[0].isalnum():
            current += " "
        current += line
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def split_fields(lines):
    if not lines:
        return [], []
    raw = "\n".join(line["text"] for line in lines)
    line_offsets, events = [], []
    context, kind, offset = "author_notes", "narrative", 0
    field_has_value = False
    inline = re.compile(r"(?<=[。；;])(" + "|".join(re.escape(label) for label in FIELD_LABELS) + r")[：:]")

    def event(start, block_kind, label=None, label_length=0):
        events.append({"start_char": start, "kind": block_kind, "context": context,
                       "field_key": FIELD_LABELS[label] if block_kind == "field" else None,
                       "label": label, "label_length": label_length})

    event(0, "narrative")
    for line_index, line in enumerate(lines):
        line_offsets.append(offset)
        value = line["text"]
        modern = value.strip("—－- ") == MODERN_HEADING
        field = FIELD_HEADING.match(value)
        if modern:
            context, kind = "modern_pharmacology", "source_section"
            event(offset, kind, MODERN_HEADING)
        elif field:
            kind = "field"
            field_has_value = bool(value[field.end():].strip())
            event(offset, kind, field[1], field.end())
        elif not value and context == "author_notes" and kind == "field":
            # An unlabelled following paragraph remains narrative. A taste
            # heading must not absorb pages of the author's later discussion.
            upcoming = next((following["text"] for following in lines[line_index + 1:] if following["text"]), "")
            continuation = re.match(r"^(?:\d+[、．.]|[（(]\d+[）)])", upcoming)
            if field_has_value and not continuation:
                kind = "narrative"
                event(offset, kind)
        elif value and kind == "field":
            field_has_value = True
        for match in inline.finditer(value):
            kind = "field"
            field_has_value = bool(value[match.end():].strip())
            event(offset + match.start(), kind, match[1], match.end() - match.start())
        offset += len(value) + 1

    # A heading can occur at character zero, replacing the initial narrative.
    starts = {item["start_char"]: item for item in events}
    events = [starts[start] for start in sorted(starts)]
    blocks, fields = [], []
    for index, block in enumerate(events):
        start = block["start_char"]
        end = events[index + 1]["start_char"] if index + 1 < len(events) else len(raw)
        block.update(order=index + 1, end_char=end, raw_text=raw[start:end])
        block["value"] = block["raw_text"][block.pop("label_length"):].strip()
        block["display_text"] = display_text(block["value"])
        first_line = bisect_right(line_offsets, start) - 1
        last_line = bisect_right(line_offsets, max(start, end - 1)) - 1
        block["source_spans"] = source_spans(lines[first_line:last_line + 1])
        blocks.append(block)
        if block["kind"] == "field":
            fields.append({key: value for key, value in block.items() if key != "kind"})
    return blocks, fields


def split_entries(pages, catalog, categories, toc_pages):
    names = {entry["id"]: normalized_name(entry["name"]) for entry in catalog}
    category_names = {normalized_name(category["heading"]) for category in categories}
    source_lines = [line for page in pages for line in page["content_lines"]]
    heading_positions = []
    for index, line in enumerate(source_lines):
        if line["pdf_page"] <= max(toc_pages):
            continue
        match = ENTRY_HEADING.fullmatch(line["text"])
        if match and names.get(match[1]) == normalized_name(match[2]):
            heading_positions.append((match[1], index))
    if [code for code, _ in heading_positions] != [entry["id"] for entry in catalog]:
        raise ValueError("Body headings do not match the complete catalog in source order")

    entries, assigned = [], {}
    for position, (code, start) in enumerate(heading_positions):
        stop = heading_positions[position + 1][1] if position + 1 < len(heading_positions) else len(source_lines)
        for index in range(start + 1, stop):
            text = source_lines[index]["text"]
            if normalized_name(text) in category_names or text.strip("—－- ") == SOURCE_END:
                stop = index
                break
        lines = trimmed_lines(source_lines[start + 1:stop])
        if not lines:
            raise ValueError(f"Entry {code} has no text")
        entry = dict(catalog[position])
        heading = source_lines[start]
        entry.update({"source_heading": heading["text"], "heading_source_line": heading["source_line"],
                      "pdf_page_start": heading["pdf_page"], "pdf_page_end": lines[-1]["pdf_page"],
                      "printed_page_end": pages[lines[-1]["pdf_page"] - 1]["printed_page"],
                      "raw_text": "\n".join(line["text"] for line in lines),
                      "source_spans": source_spans(lines)})
        entry["blocks"], entry["fields"] = split_fields(lines)
        entry["display_text"] = "\n\n".join(display_text(block["raw_text"]) for block in entry["blocks"] if block["raw_text"].strip())
        compact = re.sub(r"\s+", "", entry["raw_text"])
        entry["content_status"] = "source_omitted" if not entry["fields"] and len(compact) < 80 and re.search(r"略|没试过", compact) else "available"
        entry["references"] = []
        for match in re.finditer(r"见([^\n（）]+)[（(](\d+(?:\.\d+)?)\s*号[）)]", entry["raw_text"]):
            main, *sub = match[2].split(".")
            target = main.zfill(3) + ("." + sub[0] if sub else "")
            entry["references"].append({"target_id": target, "source_text": match[0], "target_name_in_source": match[1]})
        entries.append(entry)
        assigned[start] = {"kind": "entry_heading", "entry_id": code}
        for index in range(start + 1, stop):
            if source_lines[index]["text"]:
                assigned[index] = {"kind": "entry_text", "entry_id": code}

    # Every extracted nonempty content line has exactly one owner. This keeps
    # introductions, category headings, closing notes and the source disclaimer.
    document_blocks, current = [], None
    for index, line in enumerate(source_lines):
        if not line["text"]:
            continue
        owner = assigned.get(index, {"kind": "document", "entry_id": None})
        if (current is None or current["kind"] != owner["kind"] or current["entry_id"] != owner["entry_id"] or
                current["pdf_page"] != line["pdf_page"]):
            current = {**owner, "pdf_page": line["pdf_page"], "line_start": line["source_line"],
                       "line_end": line["source_line"], "lines": []}
            document_blocks.append(current)
        current["line_end"] = line["source_line"]
        current["lines"].append(line["text"])
    for block in document_blocks:
        block["text"] = "\n".join(block.pop("lines"))
    return entries, document_blocks


def validate_data(data):
    entries, pages = data["entries"], data["source_pages"]
    by_id = {entry["id"]: entry for entry in entries}
    checks = {
        "unique_entry_ids": len(by_id) == len(entries),
        "flat_entries": all("parent_id" not in entry for entry in entries),
        "catalog_order": [entry["display_order"] for entry in entries] == list(range(1, len(entries) + 1)),
        "catalog_body_page_match": all(pages[e["pdf_page_start"] - 1]["printed_page"] == e["printed_page_start"] for e in entries),
        "nonempty_bodies": all(entry["raw_text"].strip() for entry in entries),
        "field_partition_lossless": all("".join(b["raw_text"] for b in e["blocks"]) == e["raw_text"] for e in entries),
        "field_offsets_exact": all(e["raw_text"][b["start_char"]:b["end_char"]] == b["raw_text"] for e in entries for b in e["blocks"]),
        "field_view_matches_blocks": all(e["fields"] == [{k: v for k, v in b.items() if k != "kind"} for b in e["blocks"] if b["kind"] == "field"] for e in entries),
        "references_resolve": all(r["target_id"] in by_id for e in entries for r in e["references"]),
        "no_unicode_replacement": all("\ufffd" not in page["layout_text"] for page in pages),
    }
    original = [(p["pdf_page"], line["source_line"], line["text"])
                for p in pages for line in p["content_lines"] if line["text"]]
    recovered = []
    for block in data["document_blocks"]:
        content = pages[block["pdf_page"] - 1]["content_lines"]
        actual = [line for line in content if block["line_start"] <= line["source_line"] <= block["line_end"] and line["text"]]
        if block["text"] != "\n".join(line["text"] for line in actual):
            raise ValueError("Document block does not reproduce its source lines")
        recovered.extend((block["pdf_page"], line["source_line"], line["text"]) for line in actual)
    checks["all_content_lines_accounted_for"] = original == recovered
    for entry in entries:
        if entry["pdf_page_end"] < entry["pdf_page_start"]:
            raise ValueError(f"Reversed source span in {entry['id']}")
        for reference in entry["references"]:
            if normalized_name(by_id[reference["target_id"]]["name"]) != normalized_name(reference["target_name_in_source"]):
                checks["references_resolve"] = False
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError("Validation failed: " + ", ".join(failed))
    fields = [field for entry in entries for field in entry["fields"]]
    return {
        "schema_version": SCHEMA_VERSION, "source_sha256": data["source"]["sha256"],
        "status": "passed", "checks": checks,
        "counts": {"pdf_pages": len(pages), "entries": len(entries),
                   "main_source_numbers": sum("." not in e["id"] for e in entries),
                   "promoted_source_subentries": sum("." in e["id"] for e in entries),
                   "categories": len({entry["category"] for entry in entries}),
                   "fields": len(fields), "content_lines_accounted_for": len(original),
                   "source_omitted_entries": sum(e["content_status"] == "source_omitted" for e in entries)},
        "field_counts": dict(sorted(Counter(field["field_key"] for field in fields).items())),
        "missing_fields": {key: [e["id"] for e in entries if not any(f["field_key"] == key for f in e["fields"])]
                           for key in ("appearance", "properties_meridians", "functions_indications", "pharmacology", "toxicology")},
        "source_omissions": [{"id": e["id"], "name": e["name"], "text": e["display_text"]}
                             for e in entries if e["content_status"] == "source_omitted"],
        "unlabelled_entries": [{"id": e["id"], "name": e["name"], "pdf_page": e["pdf_page_start"]}
                               for e in entries if not e["fields"] and e["content_status"] != "source_omitted"],
        "empty_field_values": [{"id": e["id"], "label": f["label"]} for e in entries for f in e["fields"] if not f["value"]],
        "image_policy": "Image content is skipped at the user's request; coverage checks apply to the PDF text layer.",
        "pages_without_text_layer_content": [p["pdf_page"] for p in pages if not any(line["text"] for line in p["content_lines"])],
        "review_scope": "Automated structural and text-preservation checks. Source statements are transcribed, not medically verified.",
    }


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE source_pages (
    pdf_page INTEGER PRIMARY KEY, printed_page INTEGER, layout_text TEXT NOT NULL,
    content_lines_json TEXT NOT NULL, removed_layout_lines_json TEXT NOT NULL
);
CREATE TABLE catalog_categories (
    id INTEGER PRIMARY KEY, heading TEXT NOT NULL, category TEXT NOT NULL,
    section TEXT, printed_page INTEGER NOT NULL, toc_pdf_page INTEGER NOT NULL
);
CREATE TABLE entries (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, display_order INTEGER UNIQUE NOT NULL,
    category TEXT NOT NULL, section TEXT, content_status TEXT NOT NULL,
    toc_pdf_page INTEGER NOT NULL REFERENCES source_pages(pdf_page),
    pdf_page_start INTEGER NOT NULL REFERENCES source_pages(pdf_page),
    pdf_page_end INTEGER NOT NULL REFERENCES source_pages(pdf_page),
    printed_page_start INTEGER NOT NULL, printed_page_end INTEGER NOT NULL,
    source_heading TEXT NOT NULL, heading_source_line INTEGER NOT NULL,
    raw_text TEXT NOT NULL, display_text TEXT NOT NULL, source_spans_json TEXT NOT NULL
);
CREATE INDEX entries_name ON entries(name);
CREATE INDEX entries_category ON entries(category, display_order);
CREATE TABLE entry_blocks (
    entry_id TEXT NOT NULL REFERENCES entries(id), block_order INTEGER NOT NULL,
    kind TEXT NOT NULL, context TEXT NOT NULL, field_key TEXT, label TEXT,
    raw_text TEXT NOT NULL, value TEXT NOT NULL, display_text TEXT NOT NULL,
    start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, source_spans_json TEXT NOT NULL,
    PRIMARY KEY(entry_id, block_order)
);
CREATE INDEX blocks_field ON entry_blocks(field_key, entry_id);
CREATE VIEW fields AS SELECT * FROM entry_blocks WHERE kind = 'field';
CREATE TABLE entry_references (
    entry_id TEXT NOT NULL REFERENCES entries(id), target_id TEXT NOT NULL REFERENCES entries(id),
    source_text TEXT NOT NULL, target_name_in_source TEXT NOT NULL
);
CREATE TABLE document_blocks (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, entry_id TEXT REFERENCES entries(id),
    pdf_page INTEGER NOT NULL REFERENCES source_pages(pdf_page), line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL, text TEXT NOT NULL
);
CREATE VIRTUAL TABLE entries_fts USING fts5(entry_id UNINDEXED, name, body, tokenize='trigram');
"""


def json_text(value):
    return json.dumps(value, ensure_ascii=False)


def write_database(path, data):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO metadata VALUES (?, ?)",
                               [(key, json_text(data[key])) for key in ("schema_version", "source", "extraction", "field_labels")])
        for page in data["source_pages"]:
            connection.execute("INSERT INTO source_pages VALUES (?, ?, ?, ?, ?)",
                               (page["pdf_page"], page["printed_page"], page["layout_text"],
                                json_text(page["content_lines"]), json_text(page["removed_layout_lines"])))
        for index, category in enumerate(data["catalog_categories"], 1):
            connection.execute("INSERT INTO catalog_categories VALUES (?, ?, ?, ?, ?, ?)",
                               (index, category["heading"], category["category"], category["section"],
                                category["printed_page"], category["toc_pdf_page"]))
        for entry in data["entries"]:
            columns = ("id", "name", "display_order", "category", "section", "content_status", "toc_pdf_page",
                       "pdf_page_start", "pdf_page_end", "printed_page_start", "printed_page_end", "source_heading",
                       "heading_source_line", "raw_text", "display_text")
            connection.execute("INSERT INTO entries VALUES (" + ",".join("?" for _ in range(16)) + ")",
                               [entry[column] for column in columns] + [json_text(entry["source_spans"])])
            for block in entry["blocks"]:
                columns = ("order", "kind", "context", "field_key", "label", "raw_text", "value", "display_text", "start_char", "end_char")
                connection.execute("INSERT INTO entry_blocks VALUES (" + ",".join("?" for _ in range(12)) + ")",
                                   [entry["id"]] + [block[column] for column in columns] + [json_text(block["source_spans"])])
            connection.execute("INSERT INTO entries_fts VALUES (?, ?, ?)", (entry["id"], entry["name"], entry["display_text"]))
        for entry in data["entries"]:
            for reference in entry["references"]:
                connection.execute("INSERT INTO entry_references VALUES (?, ?, ?, ?)",
                                   (entry["id"], reference["target_id"], reference["source_text"], reference["target_name_in_source"]))
        for index, block in enumerate(data["document_blocks"], 1):
            connection.execute("INSERT INTO document_blocks VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (index, block["kind"], block["entry_id"], block["pdf_page"],
                                block["line_start"], block["line_end"], block["text"]))


def readonly_database(path):
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def validate_database(path, data):
    with closing(readonly_database(path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("SQLite foreign-key check failed")
        if connection.execute("SELECT count(*) FROM entries").fetchone()[0] != len(data["entries"]):
            raise ValueError("SQLite entry count differs from JSON")
        for key in ("schema_version", "source", "extraction", "field_labels"):
            row = connection.execute("SELECT value_json FROM metadata WHERE key=?", (key,)).fetchone()
            if row is None or json.loads(row[0]) != data[key]:
                raise ValueError(f"SQLite metadata mismatch for {key}")
        stored_pages = connection.execute("SELECT * FROM source_pages ORDER BY pdf_page").fetchall()
        if len(stored_pages) != len(data["source_pages"]):
            raise ValueError("SQLite source page count differs from JSON")
        for page, row in zip(data["source_pages"], stored_pages):
            for column in ("pdf_page", "printed_page", "layout_text"):
                if row[column] != page[column]:
                    raise ValueError(f"SQLite source page mismatch for {page['pdf_page']}")
            for column in ("content_lines", "removed_layout_lines"):
                if json.loads(row[column + "_json"]) != page[column]:
                    raise ValueError(f"SQLite source lines mismatch for {page['pdf_page']}")
        for entry in data["entries"]:
            row = connection.execute("SELECT * FROM entries WHERE id = ?", (entry["id"],)).fetchone()
            for column in row.keys():
                expected = json_text(entry["source_spans"]) if column == "source_spans_json" else entry[column]
                if row[column] != expected:
                    raise ValueError(f"SQLite/JSON mismatch for {entry['id']}.{column}")
            blocks = connection.execute("SELECT * FROM entry_blocks WHERE entry_id=? ORDER BY block_order", (entry["id"],)).fetchall()
            if len(blocks) != len(entry["blocks"]):
                raise ValueError(f"SQLite block count mismatch for {entry['id']}")
            for block, stored in zip(entry["blocks"], blocks):
                for column in stored.keys():
                    if column == "entry_id":
                        expected = entry["id"]
                    elif column == "block_order":
                        expected = block["order"]
                    elif column == "source_spans_json":
                        expected = json_text(block["source_spans"])
                    else:
                        expected = block[column]
                    if stored[column] != expected:
                        raise ValueError(f"SQLite field mismatch for {entry['id']}.{column}")
        stored_references = [tuple(row) for row in connection.execute(
            "SELECT entry_id,target_id,source_text,target_name_in_source FROM entry_references ORDER BY rowid"
        )]
        expected_references = [(e["id"], r["target_id"], r["source_text"], r["target_name_in_source"])
                               for e in data["entries"] for r in e["references"]]
        if stored_references != expected_references:
            raise ValueError("SQLite references differ from JSON")
        stored_document = [dict(row) for row in connection.execute(
            "SELECT kind,entry_id,pdf_page,line_start,line_end,text FROM document_blocks ORDER BY id"
        )]
        if stored_document != data["document_blocks"]:
            raise ValueError("SQLite document blocks differ from JSON")
        stored_categories = [dict(row) for row in connection.execute(
            "SELECT heading,category,section,printed_page,toc_pdf_page FROM catalog_categories ORDER BY id"
        )]
        if stored_categories != data["catalog_categories"]:
            raise ValueError("SQLite catalog categories differ from JSON")
        stored_search = [tuple(row) for row in connection.execute("SELECT entry_id,name,body FROM entries_fts ORDER BY rowid")]
        if stored_search != [(e["id"], e["name"], e["display_text"]) for e in data["entries"]]:
            raise ValueError("SQLite search content differs from JSON")


def build(pdf, output, executable):
    pdf = pdf.resolve()
    source_hash = digest(pdf.read_bytes())
    pages, text_hash = extract_pages(pdf, executable)
    catalog, categories, toc_pages = read_catalog(pages)
    entries, blocks = split_entries(pages, catalog, categories, toc_pages)
    data = {
        "schema_version": SCHEMA_VERSION,
        "source": {"file_name": pdf.name, "sha256": source_hash, "bytes": pdf.stat().st_size,
                   "pdf_pages": len(pages), "document_title": "好中醫 親試藥 / 尝药活动资料汇总",
                   "content_type": "User-supplied compilation, including personal notes and pharmacology excerpts"},
        "extraction": {"tool": "pdftotext -layout -enc UTF-8", "text_sha256": text_hash,
                       "toc_pdf_pages": toc_pages, "text_only": True,
                       "image_policy": "Image content skipped at the user's request",
                       "normalization": "LF newlines; page-edge whitespace and repeated headers/footers removed from entry text; original page layout text retained"},
        "field_labels": FIELD_LABELS, "catalog_categories": categories, "entries": entries,
        "source_pages": pages, "document_blocks": blocks,
    }
    report = validate_data(data)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="atlas-", dir=output.parent) as temporary:
        stage = Path(temporary)
        json_path, db_path = stage / "atlas.json", stage / "atlas.sqlite3"
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_database(db_path, data)
        reopened = json.loads(json_path.read_text(encoding="utf-8"))
        validate_data(reopened)
        validate_database(db_path, reopened)
        report["checks"].update({"sqlite_integrity": True, "sqlite_foreign_keys": True, "sqlite_json_all_tables_match": True})
        report["artifacts"] = {path.name: {"bytes": path.stat().st_size, "sha256": digest(path.read_bytes())}
                               for path in (json_path, db_path)}
        (stage / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for name in ("atlas.json", "atlas.sqlite3", "validation.json"):
            (stage / name).replace(output / name)
    print(json.dumps({"output": str(output.resolve()), "status": report["status"], "counts": report["counts"]}, ensure_ascii=False, indent=2))


def search(path, query, limit):
    with closing(readonly_database(path)) as connection:
        # instr supports one- and two-character Chinese names. FTS trigrams
        # accelerate longer literal searches without exposing FTS query syntax.
        if len(query) >= 3:
            literal = '"' + query.replace('"', '""') + '"'
            rows = connection.execute(
                "SELECT e.id, e.name, e.category, e.pdf_page_start FROM entries e "
                "JOIN entries_fts f ON f.entry_id=e.id WHERE entries_fts MATCH ? "
                "ORDER BY CASE WHEN e.name=? THEN 0 WHEN instr(e.name,?)>0 THEN 1 ELSE 2 END, e.display_order LIMIT ?",
                (literal, query, query, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id,name,category,pdf_page_start FROM entries WHERE instr(name,?)>0 OR instr(display_text,?)>0 "
                "ORDER BY CASE WHEN name=? THEN 0 WHEN instr(name,?)>0 THEN 1 ELSE 2 END, display_order LIMIT ?",
                (query, query, query, query, limit),
            ).fetchall()
    print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))


def show(path, entry_id, field_key):
    with closing(readonly_database(path)) as connection:
        row = connection.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown entry ID {entry_id}")
        result = dict(row)
        sql = "SELECT * FROM fields WHERE entry_id=?"
        parameters = [entry_id]
        if field_key:
            sql += " AND field_key=?"
            parameters.append(field_key)
            result.pop("raw_text")
            result.pop("display_text")
        result["fields"] = [dict(row) for row in connection.execute(sql + " ORDER BY block_order", parameters)]
    print(json.dumps(result, ensure_ascii=False, indent=2))


def validate_package(directory):
    data = json.loads((directory / "atlas.json").read_text(encoding="utf-8"))
    report = validate_data(data)
    validate_database(directory / "atlas.sqlite3", data)
    saved_report = json.loads((directory / "validation.json").read_text(encoding="utf-8"))
    if saved_report["source_sha256"] != data["source"]["sha256"]:
        raise ValueError("Validation report belongs to a different source PDF")
    for name, info in saved_report["artifacts"].items():
        content = (directory / name).read_bytes()
        if len(content) != info["bytes"] or digest(content) != info["sha256"]:
            raise ValueError(f"Artifact checksum mismatch for {name}")
    print(json.dumps({"status": "passed", "counts": report["counts"], "sqlite": "matches JSON", "checksums": "passed"}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("build")
    prepare.add_argument("--pdf", type=Path, required=True)
    prepare.add_argument("--output", type=Path, default=Path("data/herb_atlas"))
    prepare.add_argument("--pdftotext", default=shutil.which("pdftotext"))
    lookup = commands.add_parser("search")
    lookup.add_argument("query")
    lookup.add_argument("--db", type=Path, default=Path("data/herb_atlas/atlas.sqlite3"))
    lookup.add_argument("--limit", type=int, default=20)
    detail = commands.add_parser("show")
    detail.add_argument("entry_id")
    detail.add_argument("--field")
    detail.add_argument("--db", type=Path, default=Path("data/herb_atlas/atlas.sqlite3"))
    validate = commands.add_parser("validate")
    validate.add_argument("--directory", type=Path, default=Path("data/herb_atlas"))
    args = parser.parse_args()
    if args.command == "build":
        if not args.pdftotext:
            parser.error("An existing pdftotext executable is required")
        build(args.pdf, args.output, args.pdftotext)
    elif args.command == "search":
        if not args.query or args.limit < 1:
            parser.error("Search requires a nonempty query and a positive limit")
        search(args.db, args.query, args.limit)
    elif args.command == "show":
        show(args.db, args.entry_id, args.field)
    else:
        validate_package(args.directory)


if __name__ == "__main__":
    main()
