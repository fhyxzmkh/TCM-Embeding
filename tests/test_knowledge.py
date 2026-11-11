"""Regressions for PDF entry boundaries, source context and database queries."""

from contextlib import closing, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tcm_herb.knowledge import (
    FIELD_LABELS, read_catalog, readonly_database, search, split_entries,
    split_fields, validate_data, validate_database, write_database,
)


def page(number, printed, lines):
    return {"pdf_page": number, "printed_page": printed, "layout_text": "\n".join(lines),
            "removed_layout_lines": [], "content_lines": [
                {"pdf_page": number, "source_line": index + 1, "text": line}
                for index, line in enumerate(lines)
            ]}


def fixture():
    pages = [
        page(1, None, ["第一章 分类一 ........ 10", "第一节 小类 ........ 10",
                       "001.甲药 ........ 10", "001.1 乙药 ........ 11", "002.丙药 ........ 12"]),
        page(2, 10, ["第一章 分类一", "第一节 小类", "001.甲药", "口味：味道。", "",
                      "后续个人讨论。", "——现代药理研究——", "性状：第一行", "001mg/kg 是数字而非条目。"]),
        page(3, 11, ["跨页性状继续。", "药理：第一段。", "001.1 乙药", "略。"]),
        page(4, 12, ["002.丙药", "见甲药（001 号）。", "——全文完——", "结束语"]),
    ]
    catalog, categories, toc = read_catalog(pages)
    entries, blocks = split_entries(pages, catalog, categories, toc)
    return {"schema_version": 1, "source": {"sha256": "fixture"}, "extraction": {},
            "field_labels": FIELD_LABELS, "source_pages": pages, "entries": entries,
            "catalog_categories": categories, "document_blocks": blocks}


class KnowledgeTests(unittest.TestCase):
    def test_cross_page_entry_and_flat_subentry(self):
        data = fixture()
        first, child, last = data["entries"]
        self.assertEqual([e["id"] for e in data["entries"]], ["001", "001.1", "002"])
        self.assertEqual(first["pdf_page_end"], 3)
        self.assertIn("跨页性状继续", first["raw_text"])
        self.assertIn("001mg/kg", first["raw_text"])
        self.assertNotIn("001.1 乙药", first["raw_text"])
        self.assertEqual(child["content_status"], "source_omitted")
        self.assertNotIn("全文完", last["raw_text"])
        self.assertTrue(validate_data(data)["checks"]["all_content_lines_accounted_for"])

    def test_author_paragraphs_do_not_become_taste_or_pharmacology(self):
        first = fixture()["entries"][0]
        taste = next(f for f in first["fields"] if f["field_key"] == "taste_notes")
        self.assertEqual(taste["value"], "味道。")
        self.assertEqual(taste["context"], "author_notes")
        self.assertTrue(any(b["kind"] == "narrative" and "后续个人讨论" in b["value"] for b in first["blocks"]))
        appearance = next(f for f in first["fields"] if f["field_key"] == "appearance")
        self.assertEqual(appearance["context"], "modern_pharmacology")
        self.assertIn("跨页性状继续", appearance["value"])

    def test_inline_fields_and_source_character_offsets(self):
        lines = page(1, 10, ["主要功效：甲；继发功效：乙。", "", "另起一段。"])["content_lines"]
        blocks, fields = split_fields(lines)
        raw = "\n".join(line["text"] for line in lines)
        self.assertEqual([f["field_key"] for f in fields], ["reported_effects", "secondary_effects"])
        self.assertEqual(fields[1]["value"], "乙。")
        self.assertEqual("".join(b["raw_text"] for b in blocks), raw)
        for block in blocks:
            self.assertEqual(raw[block["start_char"]:block["end_char"]], block["raw_text"])

    def test_standalone_heading_keeps_its_numbered_paragraphs(self):
        lines = page(1, 10, ["功效：", "", "1.第一条。", "", "2.第二条。", "", "无标签的后续讨论。"])["content_lines"]
        blocks, fields = split_fields(lines)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["value"], "1.第一条。\n\n2.第二条。")
        self.assertTrue(any(b["kind"] == "narrative" and "后续讨论" in b["value"] for b in blocks))

    def test_forward_references_and_literal_chinese_search(self):
        data = fixture()
        # Point an earlier entry at a later one to exercise deferred insertion.
        data["entries"][0]["references"] = [
            {"target_id": "002", "source_text": "见丙药（002 号）", "target_name_in_source": "丙药"}
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "atlas.sqlite3"
            write_database(path, data)
            validate_database(path, data)
            for query in ("甲", "甲药", "个人讨论"):
                output = StringIO()
                with redirect_stdout(output):
                    search(path, query, 10)
                self.assertEqual(json.loads(output.getvalue())[0]["id"], "001")
            with closing(readonly_database(path)) as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM entries")

    def test_validator_rejects_missing_and_crossed_text(self):
        data = fixture()
        data["entries"][0]["blocks"][0]["raw_text"] = "lost text"
        with self.assertRaisesRegex(ValueError, "Validation failed"):
            validate_data(data)
        data = fixture()
        data["document_blocks"].pop()
        with self.assertRaisesRegex(ValueError, "all_content_lines_accounted_for"):
            validate_data(data)


if __name__ == "__main__":
    unittest.main()
