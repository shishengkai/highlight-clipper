"""字幕导入、绑定和边界行为的回归测试。"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/highlight-clipper/scripts"))
from hc import transcript


SOURCE_HASH = "a" * 64


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def load(self, value, suffix=".json", duration=5000):
        path = self.root / ("transcript" + suffix)
        path.write_text(json.dumps(value, ensure_ascii=False) if suffix == ".json" else value,
                        encoding="utf-8")
        return transcript.normalize(path, SOURCE_HASH, duration)

    @staticmethod
    def source():
        return {"sentences": [
            {"id": 1, "start_ms": 100, "end_ms": 1600, "text": "Hello, wonderful world!",
             "speaker_id": 0, "words": [
                 {"begin_time": 100, "end_time": 400, "text": "Hello", "punctuation": ","},
                 {"begin_time": 500, "end_time": 1000, "text": "wonderful"},
                 {"begin_time": 1100, "end_time": 1500, "text": "world", "punctuation": "!"}]},
            {"id": 2, "start_ms": 1700, "end_ms": 2900, "text": "我喜欢 AI，也喜欢你。",
             "speaker_id": 1, "words": [
                 {"begin_time": 1700, "end_time": 1800, "text": "我"},
                 {"begin_time": 1800, "end_time": 2000, "text": "喜欢"},
                 {"begin_time": 2000, "end_time": 2300, "text": "AI", "punctuation": "，"},
                 {"begin_time": 2300, "end_time": 2400, "text": "也"},
                 {"begin_time": 2400, "end_time": 2700, "text": "喜欢"},
                 {"begin_time": 2700, "end_time": 2900, "text": "你", "punctuation": "。"}]}]}

    def test_srt_preserves_overlap_and_chinese(self):
        data = self.load("1\n00:00:00,000 --> 00:00:01,200\n你好，世界。\n\n"
                         "2\n00:00:00,900 --> 00:00:02,000\n<b>Hello &amp; welcome.</b>\n", ".srt")
        self.assertEqual(data["precision"], "subtitle")
        self.assertEqual(data["sentences"][0]["end_ms"], 1200)
        self.assertEqual(data["sentences"][1]["start_ms"], 900)
        self.assertEqual(data["sentences"][1]["text"], "Hello & welcome.")
        output = self.root / "full.srt"
        transcript.write_srt(data, output)
        self.assertIn("你好，世界。", output.read_text())

    def test_vtt_voice_inline_time_and_metadata(self):
        data = self.load("WEBVTT\nLanguage: en\n\nNOTE a comment\nmetadata\n\n"
                         "first-cue\n00:00.100 --> 00:01.000 align:start\n"
                         "<v Guest>Hello <00:00.500><i>world</i>.</v>\n", ".vtt")
        self.assertEqual(data["sentences"][0]["text"], "Hello world.")
        self.assertEqual(data["sentences"][0]["speaker_id"], "Guest")

    def test_unrecognized_srt_block_is_not_dropped(self):
        for value in ("unexpected line", "1\n00:00:00,000 --> 00:00:01,000\nHi\n\nunrecognized",
                      "1\n00:00:60,000 --> 00:01:01,000\nBad seconds",
                      "1\n00:00:00,000 --> 00:00:01,000\nHi\n2\n00:00:01,000 --> 00:00:02,000\nNext"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value, ".srt", duration=70000)

    def test_vtt_header_does_not_swallow_first_cue(self):
        with self.assertRaises(ValueError):
            self.load("WEBVTT\n00:00.000 --> 00:01.000\nLost cue", ".vtt")

    def test_empty_or_out_of_order_or_out_of_bounds_rejected(self):
        for value in ({"sentences": []},
                      {"sentences": [{"start_ms": 100, "end_ms": 5001, "text": "too long"}]},
                      {"sentences": [{"start_ms": 500, "end_ms": 1000, "text": "first"},
                                     {"start_ms": 100, "end_ms": 200, "text": "second"}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)

    def test_existing_source_binding_and_ids_checked(self):
        for key, value in (("source_sha256", "b" * 64), ("source_duration_ms", 6000),
                           ("timeline_origin_ms", 100)):
            original = self.source()
            original[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(original)
        original = self.source()
        original["sentences"][1]["id"] = 7
        with self.assertRaises(ValueError):
            self.load(original)

    def test_word_times_never_cross_sentence_boundaries(self):
        for begin, end in ((99, 400), (100, 1601), (400, 300), (100, 100), (True, 200)):
            original = self.source()
            original["sentences"][0]["words"][0].update(begin_time=begin, end_time=end)
            with self.subTest(begin=begin, end=end), self.assertRaises(ValueError):
                self.load(original)

    def test_dashscope_single_track_and_multichannel_refusal(self):
        sentence = {"begin_time": 0, "end_time": 1000, "text": "Hello", "words": []}
        data = self.load({"transcripts": [{"channel_id": 0, "sentences": [sentence]}]})
        self.assertEqual(data["provider"], "dashscope")
        self.assertEqual(data["precision"], "sentence")
        with self.assertRaisesRegex(ValueError, "单一音轨"):
            self.load({"transcripts": [{"channel_id": 0, "sentences": [sentence]},
                                      {"channel_id": 1, "sentences": [sentence]}]})

    def test_partial_english_keeps_spaces_and_punctuation(self):
        data = self.load(self.source())
        result = transcript.clip_srt(data, 450, 1600)
        self.assertIn("wonderful world!", result)
        self.assertIn("00:00:00,050 --> 00:00:01,050", result)
        self.assertNotIn("Hello", result)
        self.assertNotIn("我", result)

    def test_partial_chinese_preserves_original_mixed_spaces(self):
        data = self.load(self.source())
        result = transcript.clip_srt(data, 1800, 2300)
        self.assertIn("喜欢 AI，", result)
        self.assertNotIn("也", result)
        self.assertNotIn("喜 欢", result)

    def test_straddling_words_are_excluded_and_next_sentence_does_not_leak(self):
        data = self.load(self.source())
        result = transcript.clip_srt(data, 250, 1200)
        self.assertIn("wonderful", result)
        self.assertNotIn("Hello", result)
        self.assertNotIn("world", result)
        self.assertNotIn("我", result)

    def test_partial_sentence_without_words_fails_explicitly(self):
        data = self.load("1\n00:00:00,000 --> 00:00:02,000\nwhole sentence\n", ".srt")
        with self.assertRaisesRegex(ValueError, "词级"):
            transcript.clip_srt(data, 500, 2000)
        self.assertIn("whole sentence", transcript.clip_srt(data, 0, 2000))

    def test_word_alignment_fallback_joins_english(self):
        original = self.source()
        original["sentences"][0]["text"] = "HELLO WONDERFUL WORLD"
        data = self.load(original)
        self.assertIn("wonderful world!", transcript.clip_srt(data, 450, 1600))

    def test_noncontiguous_word_selection_never_restores_excluded_word(self):
        data = self.load({"sentences": [{"start_ms": 0, "end_ms": 1000,
                          "text": "one two three", "words": [
                              {"start_ms": 0, "end_ms": 100, "text": "one"},
                              {"start_ms": 50, "end_ms": 900, "text": "two"},
                              {"start_ms": 100, "end_ms": 200, "text": "three"}]}]})
        result = transcript.clip_srt(data, 0, 300)
        self.assertIn("one three", result)
        self.assertNotIn("two", result)

    def test_flat_multichannel_input_is_rejected(self):
        original = self.source()
        original["sentences"][0]["channel_id"] = 0
        original["sentences"][1]["channel_id"] = 1
        with self.assertRaisesRegex(ValueError, "多个声道"):
            self.load(original)

    def test_mixed_precision_and_false_claim(self):
        original = self.source()
        original["sentences"][1]["words"] = []
        self.assertEqual(self.load(original)["precision"], "sentence")
        original["precision"] = "word"
        with self.assertRaises(ValueError):
            self.load(original)

    def test_normalized_roundtrip_and_complete_reading(self):
        data = self.load(self.source())
        self.assertEqual(data, self.load(copy.deepcopy(data)))
        output = self.root / "reading.txt"
        transcript.write_reading(data, output)
        text = output.read_text()
        self.assertIn("S0001", text)
        self.assertIn("S0002", text)
        self.assertIn("我喜欢 AI，也喜欢你。", text)


if __name__ == "__main__":
    unittest.main()
