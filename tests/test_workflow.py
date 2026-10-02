"""用合成素材验证公开 CLI 全流程，不访问外部 API、不安装技能。"""

import contextlib
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/highlight-clipper/scripts"
sys.path.insert(0, str(SCRIPTS))
from hc import cli, core, delivery  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 FFmpeg/ffprobe")
class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory(prefix="highlight-测试 ")
        cls.source = Path(cls.base.name) / "synthetic.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=3",
                        "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=3", "-c:v", "libx264",
                        "-c:a", "aac", "-shortest", str(cls.source)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="case ", dir=self.base.name)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.srt = self.root / "source.srt"
        def stamp(ms):
            return f"00:00:{ms//1000:02},{ms%1000:03}"
        self.srt.write_text("\n\n".join(f"{i}\n{stamp((i-1)*500)} --> {stamp(i*500)}\nSentence {i}." for i in range(1, 7)) + "\n")
        self.call("prepare", "--video", self.source, "--project", self.project, "--subtitles", self.srt,
                  "--title", '<script>alert("test")</script>')
        self.plan_path = self.project / "analysis.json"
        self.editorial = json.loads((ROOT / "skills/highlight-clipper/assets/example-plan.json").read_text())
        self.editorial["source_sha256"] = core.fingerprint(self.source)
        self.editorial["transcript_sha256"] = core.fingerprint(self.project / "transcript.json")
        core.write_json(self.plan_path, self.editorial)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args, ok=True):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = cli.main(list(map(str, args)))
        values = [json.loads(line) for line in stdout.getvalue().splitlines()]
        if ok:
            self.assertEqual(result, 0, stdout.getvalue())
        else:
            self.assertNotEqual(result, 0, stdout.getvalue())
        return values[-1], values

    def action(self, name, *extra, ok=True):
        return self.call(name, "--project", self.project, "--plan", self.plan_path, *extra, ok=ok)

    def test_full_render_resume_preview_feedback_and_montage(self):
        self.action("validate")
        result, _ = self.action("render")
        self.assertEqual(result["rendered"], 3)
        output = self.project / "clips"
        first = output / "c01/clip.mp4"
        original = (first.stat().st_mtime_ns, core.fingerprint(first))
        _, events = self.action("render")
        self.assertEqual(sum(e.get("status") == "cached" for e in events), 3)
        self.assertEqual((first.stat().st_mtime_ns, core.fingerprint(first)), original)
        page = (output / "index.html").read_text()
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn('<script>alert("test")</script>', page)
        self.assertIn("c02_answer/clip.mp4", page)
        self.call("feedback", "--project", self.project, "--id", "c01", "--label", "必选", "--note", "很好")
        self.call("feedback", "--project", self.project, "--id", "c01", "--label", "可选", "--note", "保留")
        self.action("report")
        self.assertEqual(len(core.read_json(self.project / "feedback.json")["events"]), 2)
        self.assertIn("保留", (output / "index.html").read_text())
        montage = self.root / "montage.mp4"
        self.action("montage", "--ids", "c01", "c02", "--output", montage)
        mapping = core.read_json(montage.with_suffix(".manifest.json"))
        self.assertEqual([x["id"] for x in mapping["segments"]], ["c01", "c02"])
        self.action("montage", "--ids", "c02", "c02_answer", "--output", self.root / "overlap.mp4", ok=False)

    def test_partial_render_keeps_all_backups_in_report(self):
        result, _ = self.action("render", "--ids", "c01")
        self.assertEqual((result["candidates"], result["rendered"]), (3, 1))
        data = core.read_json(self.project / "clips/delivery.json")
        self.assertEqual(len(data["clips"]), 3)
        self.assertFalse(data["clips"][2]["rendered"])

    def test_replacing_transcript_preserves_history_and_invalidates_plan(self):
        self.srt.write_text(self.srt.read_text().replace("Sentence 1.", "Changed sentence."))
        self.call("import-transcript", "--project", self.project, "--input", self.srt, ok=False)
        self.call("import-transcript", "--project", self.project, "--input", self.srt, "--replace")
        self.assertEqual(len(list((self.project / "history").glob("*.json"))), 1)
        result, _ = self.action("validate", ok=False)
        self.assertIn("转写已经变化", result["detail"])

    def test_missing_derived_transcript_files_are_repaired(self):
        (self.project / "transcript-reading.txt").unlink()
        self.call("import-transcript", "--project", self.project, "--input", self.srt)
        self.assertTrue((self.project / "transcript-reading.txt").is_file())

    def test_output_edit_is_detected_before_cached_reuse(self):
        self.action("render", "--ids", "c01")
        subtitle = self.project / "clips/c01/clip.auto.srt"
        subtitle.write_text("modified")
        result, _ = self.action("render", "--ids", "c01", ok=False)
        self.assertIn("被修改", result["detail"])

    def test_changed_cut_uses_new_output_version(self):
        self.action("render", "--ids", "c01")
        self.editorial["candidates"][0]["range"]["start_id"] = 2
        core.write_json(self.plan_path, self.editorial)
        self.action("render", "--ids", "c01", ok=False)
        self.action("render", "--ids", "c01", "--output", self.project / "clips-v2")

    def test_csv_feedback_is_preserved_when_new_report_generated(self):
        self.action("report")
        csv_path = self.project / "clips/feedback.csv"
        with csv_path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[2]["label"], rows[2]["note"] = "选点对但范围不对", "多留一个问题"
        with csv_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        self.action("report")
        self.assertIn("多留一个问题", csv_path.read_text())

    def test_old_feedback_does_not_follow_reused_id_for_new_cut(self):
        self.call("feedback", "--project", self.project, "--id", "c01", "--label", "必选")
        self.editorial["candidates"][0]["range"]["start_id"] = 2
        core.write_json(self.plan_path, self.editorial)
        self.action("report")
        data = core.read_json(self.project / "clips/delivery.json")
        self.assertEqual(data["clips"][0]["feedback"]["label"], "")
        self.assertEqual(core.read_json(self.project / "feedback.json")["events"][0]["label"], "必选")

    def test_csv_and_command_conflict_preserves_both_opinions(self):
        self.action("report")
        csv_path = self.project / "clips/feedback.csv"
        with csv_path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["label"] = "不选"
        with csv_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        self.call("feedback", "--project", self.project, "--id", "c01", "--label", "必选")
        result, _ = self.action("report")
        self.assertEqual(result["new_feedback_conflicts"], 1)
        data = core.read_json(self.project / "clips/delivery.json")
        self.assertEqual(data["clips"][0]["feedback"]["label"], "不选")
        conflict = core.read_json(self.project / "clips/feedback-conflicts.json")["events"][0]
        self.assertEqual(conflict["command"]["label"], "必选")
        self.action("report")
        self.assertEqual(len(core.read_json(self.project / "clips/feedback-conflicts.json")["events"]), 1)

    def test_montage_recovers_publication_before_manifest_without_reencoding(self):
        self.action("render", "--ids", "c01", "c02")
        montage = self.root / "recover.mp4"
        original_link = os.link
        def link(source, destination, *args, **kwargs):
            if str(destination).endswith(".manifest.json"):
                raise OSError("模拟发布映射前中断")
            return original_link(source, destination, *args, **kwargs)
        with patch.object(delivery.os, "link", side_effect=link):
            self.action("montage", "--ids", "c01", "c02", "--output", montage, ok=False)
        self.assertTrue(montage.exists())
        self.assertFalse(montage.with_suffix(".manifest.json").exists())
        with patch.object(delivery.media, "concat", side_effect=AssertionError("不应再次编码")):
            self.action("montage", "--ids", "c01", "c02", "--output", montage)
            result, _ = self.action("montage", "--ids", "c01", "c02", "--output", montage)
        self.assertEqual(result["status"], "cached")

    def test_project_lock_refuses_parallel_writer(self):
        with core.project_lock(self.project):
            result, _ = self.action("validate", ok=False)
            self.assertIn("另一进程", result["detail"])

    def test_asr_success_imports_normalized_result_and_archives_subtitles(self):
        sentences = [{"begin_time": (i - 1) * 500, "end_time": i * 500, "text": f"ASR {i}.",
                      "speaker_id": 0, "words": []} for i in range(1, 7)]
        with patch("hc.providers.transcribe", return_value={"status": "completed", "raw_transcript": {
                "transcripts": [{"channel_id": 0, "sentences": sentences}]}}):
            result, _ = self.call("transcribe", "--project", self.project, "--replace")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(core.read_json(self.project / "transcript.json")["provider"], "dashscope")
        self.assertNotIn("raw_transcript", result)

    def test_skill_can_run_from_copied_directory_without_installation(self):
        copied = self.root / "单独技能"
        shutil.copytree(ROOT / "skills/highlight-clipper", copied)
        result = subprocess.run([sys.executable, str(copied / "scripts/highlight_clipper.py"), "read",
                                 "--project", str(self.project), "--start-id", "2", "--end-id", "3"],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["returned_range"], [2, 3])


class CoreSafetyTests(unittest.TestCase):
    def test_error_redaction_removes_env_secrets_and_urls(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-secret-value"}):
            result = core.redact("test-secret-value https://example.test/?secret=x")
        self.assertNotIn("test-secret-value", result)
        self.assertNotIn("secret=x", result)

    def test_source_url_retains_video_identity_without_tokens(self):
        self.assertEqual(core.source_url("https://www.youtube.com/watch?v=abcdefghijk&token=private"),
                         "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(core.source_url("https://example.test/video.mp4?token=private"),
                         "https://example.test/video.mp4")


if __name__ == "__main__":
    unittest.main()
