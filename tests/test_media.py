"""用真实两秒素材验证切点、输出完整性以及拒绝覆盖。"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/highlight-clipper/scripts"))
from hc import media


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 FFmpeg 和 ffprobe")
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.source = Path(cls.shared.name) / "two-seconds.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
            "color=c=red:s=160x90:r=25:d=1[a];color=c=blue:s=160x90:r=25:d=1[b];[a][b]concat=n=2:v=1:a=0",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "250", "-sc_threshold", "0",
            "-c:a", "aac", "-shortest", str(cls.source)
        ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def pixel(path, seconds):
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(path), "-ss", str(seconds), "-frames:v", "1",
            "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"
        ], check=True, capture_output=True)
        return tuple(result.stdout[:3])

    def test_probe_cut_exact_non_keyframe_and_complete_decode(self):
        original = media.probe(self.source)
        self.assertEqual(original["duration_ms"], 2000)
        self.assertTrue(original["has_audio"] and original["has_video"])
        destination = self.root / "clip.mp4"
        report = media.cut(self.source, destination, 760, 1240)
        self.assertLessEqual(abs(report["duration_ms"] - 480), 40)
        self.assertEqual((report["width"], report["height"]), (160, 90))
        self.assertEqual(report["bytes"], destination.stat().st_size)
        self.assertEqual(report["sha256"], media.sha256(destination))
        # 起点不在关键帧上，切片前半是红色、后半是蓝色。
        red, _, blue = self.pixel(destination, 0.04)
        self.assertGreater(red, blue + 150)
        red, _, blue = self.pixel(destination, 0.36)
        self.assertGreater(blue, red + 150)
        media.decode_check(destination)
        self.assertEqual(list(self.root.glob(".hc-*")), [])

    def test_audio_format_and_frame(self):
        audio = media.extract_audio(self.source, self.root / "audio.wav")
        with wave.open(str(audio), "rb") as stream:
            self.assertEqual(stream.getframerate(), 16000)
            self.assertEqual(stream.getnchannels(), 1)
            self.assertEqual(stream.getsampwidth(), 2)
            self.assertAlmostEqual(stream.getnframes() / 16000, 2, delta=0.1)
        image = media.frame(self.source, self.root / "frame.png", 1200)
        self.assertTrue(image.read_bytes().startswith(b"\x89PNG"))

    def test_existing_and_broken_symlink_outputs_are_preserved(self):
        target = self.root / "clip.mp4"
        target.write_bytes(b"keep this")
        with self.assertRaises(FileExistsError):
            media.cut(self.source, target, 0, 1000)
        self.assertEqual(target.read_bytes(), b"keep this")
        target.unlink()
        target.symlink_to(self.root / "missing.mp4")
        with self.assertRaises(FileExistsError):
            media.cut(self.source, target, 0, 1000)
        self.assertTrue(target.is_symlink())

    def test_bad_inputs_ranges_and_missing_audio(self):
        invalid = self.root / "bad.mp4"
        invalid.write_bytes(b"not a video")
        with self.assertRaises(RuntimeError):
            media.probe(invalid)
        with self.assertRaises(RuntimeError):
            media.decode_check(invalid)
        for start, end in ((-1, 1000), (1000, 1000), (1000, 2001), (0.0, 1000), (True, 1000)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                media.cut(self.source, self.root / "clip.mp4", start, end)
        audio_only = media.extract_audio(self.source, self.root / "audio.wav")
        with self.assertRaisesRegex(ValueError, "画面和音频"):
            media.cut(audio_only, self.root / "clip.mp4", 0, 1000)

    def test_failed_verification_does_not_publish_partial_result(self):
        destination = self.root / "clip.mp4"
        with mock.patch.object(media, "decode_check", side_effect=RuntimeError("模拟解码错误")):
            with self.assertRaises(RuntimeError):
                media.cut(self.source, destination, 0, 1000)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".hc-*")), [])

    def test_nonzero_time_origin_is_rejected(self):
        shifted = self.root / "shifted.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.source), "-c", "copy",
                        "-output_ts_offset", "1", str(shifted)], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "时间原点"):
            media.probe(shifted)

    def test_concat_preserves_requested_order_and_total_duration(self):
        # 引号、空格和括号不应被解释为 shell 或 filter 语法。
        red = self.root / "red ' [first].mp4"
        blue = self.root / "blue second.mp4"
        red_info = media.cut(self.source, red, 0, 400)
        blue_info = media.cut(self.source, blue, 1200, 1600)
        output = self.root / "montage.mp4"
        report = media.concat([blue, red], output)
        self.assertLessEqual(abs(report["duration_ms"] - red_info["duration_ms"]
                                 - blue_info["duration_ms"]), 100)
        r, _, b = self.pixel(output, 0.1)
        self.assertGreater(b, r + 150)
        r, _, b = self.pixel(output, 0.6)
        self.assertGreater(r, b + 150)
        self.assertEqual(report["sha256"], media.sha256(output))
        with self.assertRaises(FileExistsError):
            media.concat([red, blue], output)

    def test_concat_refuses_empty_or_mixed_dimensions(self):
        with self.assertRaises(ValueError):
            media.concat([], self.root / "montage.mp4")
        first = media.probe(self.source)
        second = dict(first, width=320)
        with mock.patch.object(media, "probe", side_effect=[first, second]):
            with self.assertRaisesRegex(ValueError, "尺寸不一致"):
                media.concat([self.source, self.source], self.root / "montage.mp4")


class MediaSafetyTests(unittest.TestCase):
    def test_subprocess_failure_does_not_expose_diagnostic_secrets(self):
        process = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"https://example.invalid?key=SECRET")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "file.mp4"
            source.write_bytes(b"placeholder")
            with mock.patch.object(media.subprocess, "run", return_value=process):
                with self.assertRaises(RuntimeError) as caught:
                    media.probe(source)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("https", str(caught.exception))

    def test_publish_race_keeps_existing_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "temporary", Path(folder) / "target"
            source.write_bytes(b"new")
            target.write_bytes(b"old")
            with self.assertRaises(FileExistsError):
                media._publish(source, target)
            self.assertEqual(target.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
