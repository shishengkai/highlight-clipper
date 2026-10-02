"""编辑计划的来源身份、全文覆盖、上下文变体和错误时间测试。"""

import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/highlight-clipper/scripts"))
from hc import plan  # noqa: E402


def sample():
    editorial = json.loads((ROOT / "skills/highlight-clipper/assets/example-plan.json").read_text())
    metadata = {"source": {"sha256": "0" * 64, "duration_ms": 6000}}
    transcript = {"sentences": [{"id": i, "start_ms": (i - 1) * 1000, "end_ms": i * 1000,
                                  "text": f"第 {i} 句", "words": []} for i in range(1, 7)]}
    return editorial, metadata, transcript


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.p, self.m, self.t = sample()

    def compile(self):
        return plan.compile_plan(self.p, self.m, self.t, "0" * 64)

    def test_variants_backups_overlap_and_question_context_preserved(self):
        result = self.compile()
        self.assertEqual(len(result["candidates"]), 3)
        short = result["candidates"][2]
        self.assertEqual(short["priority"], "备选")
        self.assertFalse(short["question_fully_included"])
        self.assertEqual(short["variant_of"], "c02")
        self.assertEqual(short["overlaps"], [{"id": "c02", "duration_ms": 2000}])

    def test_identity_mismatch_rejects_equal_duration_media(self):
        self.p["source_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "不同的源视频"):
            self.compile()

    def test_changed_transcript_rejects_stale_sentence_references(self):
        self.p["transcript_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "转写已经变化"):
            self.compile()

    def test_coverage_gap_overlap_and_out_of_order_all_fail(self):
        for first in (3, 5):
            with self.subTest(first=first):
                self.p["topics"][1]["start_id"] = first
                with self.assertRaisesRegex(ValueError, "完整覆盖"):
                    self.compile()
        self.p, _, _ = sample()
        self.p["topics"].reverse()
        with self.assertRaisesRegex(ValueError, "完整覆盖"):
            self.compile()

    def test_core_cannot_escape_context(self):
        self.p["candidates"][0]["core"] = {"start_id": 2, "end_id": 4}
        with self.assertRaisesRegex(ValueError, "完整包含核心"):
            self.compile()

    def test_unsafe_duplicate_and_unknown_ids_fail(self):
        for value in ("../other", "/tmp/overwrite", "c02"):
            self.p, _, _ = sample()
            self.p["candidates"][0]["id"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.compile()
        self.p, _, _ = sample()
        self.p["first_watch"] = ["nonexistent"]
        with self.assertRaises(ValueError):
            self.compile()

    def test_variants_cannot_cycle_or_point_to_missing_candidate(self):
        for parent in ("missing", "c02_answer", "c01"):
            self.p, _, _ = sample()
            self.p["candidates"][2]["variant_of"] = parent
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                self.compile()

    def test_unselected_topics_require_audit_but_no_fixed_candidate_count(self):
        self.p["candidates"] = []
        self.p["first_watch"] = []
        for t in self.p["topics"]:
            t["candidate_ids"] = []
        self.assertEqual(self.compile()["candidates"], [])
        self.p["topics"][0]["audit_note"] = ""
        with self.assertRaises(ValueError):
            self.compile()

    def test_word_indices_map_actual_times_and_reject_missing_words(self):
        r = {"start_id": 1, "end_id": 1, "start_word": 2, "end_word": 3}
        with self.assertRaisesRegex(ValueError, "没有词级时间"):
            plan.resolve_range(r, self.t["sentences"], "test")
        self.t["sentences"][0]["words"] = [{"start_ms": a, "end_ms": b, "text": "x"}
                                              for a, b in ((10, 100), (200, 300), (400, 500))]
        self.assertEqual(plan.resolve_range(r, self.t["sentences"], "test")[:2], (200, 500))

    def test_manual_boundary_needs_evidence_and_cannot_cut_core(self):
        c = self.p["candidates"][0]
        c["boundary_override"] = {"start_ms": 0, "end_ms": 2500, "reason": "检查", "evidence": "回看"}
        with self.assertRaisesRegex(ValueError, "完整包含核心"):
            self.compile()
        c["boundary_override"]["end_ms"] = 3000
        c["boundary_override"]["evidence"] = ""
        with self.assertRaises(ValueError):
            self.compile()

    def test_overlap_end_is_not_silently_truncated(self):
        sentences = copy.deepcopy(self.t["sentences"][:2])
        sentences[0]["end_ms"] = 2500
        self.assertEqual(plan.resolve_range({"start_id": 1, "end_id": 2}, sentences, "字幕")[1], 2500)

    def test_manual_expansion_keeps_declared_and_actual_sentence_ids(self):
        self.p["candidates"][0].update(range={"start_id": 2, "end_id": 3},
                                      boundary_override={"start_ms": 0, "end_ms": 3000, "reason": "保留问题", "evidence": "回读原文"})
        result = self.compile()["candidates"][0]
        self.assertEqual(result["declared_sentence_ids"], [2, 3])
        self.assertEqual(result["sentence_ids"], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
