"""不联网、不付费的协议与恢复测试。"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/highlight-clipper/scripts"))
from hc import providers as p


class Response(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None):
        super().__init__(data)
        self.status = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "input.mp4"
        self.source.write_bytes(b"fake-original-media")
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.out = self.root / "project"
        self.environment = patch.dict(os.environ, {"DASHSCOPE_API_KEY": "secret-from-process", "SNAPANY_API_KEY": "snap-secret", "PATH": os.environ.get("PATH", "")}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def transcribe(self, **kwargs):
        with patch("hc.media.extract_audio", side_effect=self.fake_audio), patch.object(p, "_verify_audio_url"):
            return p.transcribe(self.source, self.out, self.sha, 10_000,
                                audio_url="https://media.example/audio.wav?signature=hidden", **kwargs)

    def fake_audio(self, source, destination):
        destination.write_bytes(b"prepared-wave")
        return destination

    def pending(self):
        with patch.object(p, "_json_request", side_effect=[
            {"output": {"task_id": "test-task", "task_status": "PENDING"}},
            {"output": {"task_status": "RUNNING"}},
        ]):
            return self.transcribe()

    def test_missing_key_does_not_read_config_or_write_job(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(p, "_json_request") as network:
            with self.assertRaisesRegex(p.ProviderError, "DASHSCOPE_API_KEY"):
                self.transcribe()
        network.assert_not_called()
        self.assertFalse((self.out / "asr-job.json").exists())

    def test_submit_contract_and_durable_guard(self):
        calls = []

        def request(url, **kwargs):
            calls.append((url, kwargs))
            if kwargs.get("method") == "POST":
                state = json.loads((self.out / "asr-job.json").read_text())
                self.assertEqual(state["status"], "submitting")
                self.assertNotIn("signature", json.dumps(state))
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-from-process")
                self.assertEqual(kwargs["headers"]["X-DashScope-Async"], "enable")
                self.assertEqual(kwargs["payload"]["parameters"]["channel_id"], [0])
                self.assertEqual(kwargs["payload"]["parameters"]["language_hints"], ["en"])
                self.assertEqual(kwargs["payload"]["model"], p.DEFAULT_ASR_MODEL)
                return {"output": {"task_id": "task-123"}}
            return {"output": {"task_status": "RUNNING"}}

        with patch.object(p, "_json_request", side_effect=request):
            self.assertEqual(self.transcribe()["status"], "running")
            self.assertEqual(self.transcribe()["status"], "running")
        self.assertEqual(sum(k.get("method") == "POST" for _, k in calls), 1)
        text = (self.out / "asr-job.json").read_text()
        self.assertNotIn("secret-from-process", text)
        self.assertNotIn("signature", text)

    def test_unknown_submission_never_resubmits(self):
        with patch.object(p, "_json_request", side_effect=p.ProviderError("外部服务网络请求失败")) as network:
            self.assertEqual(self.transcribe()["status"], "submission_unknown")
            self.assertEqual(self.transcribe()["status"], "submission_unknown")
        self.assertEqual(network.call_count, 1)

    def test_crash_during_submission_never_resubmits(self):
        with patch.object(p, "_json_request", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.transcribe()
        with patch.object(p, "_json_request") as network:
            self.assertEqual(self.transcribe()["status"], "submitting")
        network.assert_not_called()

    def test_submit_429_is_saved_and_not_automatically_retried(self):
        with patch.object(p, "_json_request", side_effect=p.ProviderError("外部服务 HTTP 429", status_code=429)) as network:
            result = self.transcribe()
            self.assertEqual(result["status"], "submit_rejected")
            self.assertEqual(result["http_status"], 429)
            self.transcribe()
        self.assertEqual(network.call_count, 1)

    def test_poll_429_keeps_existing_task(self):
        self.pending()
        with patch.object(p, "_json_request", side_effect=p.ProviderError("外部服务 HTTP 429", status_code=429)) as network:
            result = self.transcribe()
        self.assertEqual(result["task_id"], "test-task")
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["last_poll_error"], "http_429")
        self.assertNotIn("method", network.call_args.kwargs)

    def test_failed_subtask_is_terminal(self):
        self.pending()
        with patch.object(p, "_json_request", return_value={"output": {"task_status": "SUCCEEDED", "results": [{"subtask_status": "FAILED"}]}}) as network:
            self.assertEqual(self.transcribe()["status"], "failed")
            self.assertEqual(self.transcribe()["status"], "failed")
        self.assertEqual(network.call_count, 1)

    def test_completed_result_removes_urls_preserves_words_and_usage(self):
        self.pending()
        raw = {"file_url": "https://secret.example/?signature=hidden", "transcripts": [
            {"text": "Hello", "sentences": [{"begin_time": 1, "end_time": 900, "speaker_id": 0,
             "text": "Hello", "words": [{"begin_time": 1, "end_time": 900, "text": "Hello"}]}]}]}
        with patch.object(p, "_json_request", side_effect=[
            {"output": {"task_status": "SUCCEEDED", "results": [{"subtask_status": "SUCCEEDED", "transcription_url": "https://results.example/?signature=hidden"}]},
             "usage": {"duration": 9.1, "input_tokens": 40}}, raw,
        ]):
            result = self.transcribe()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["input_tokens"], 40)
        self.assertNotIn("file_url", result["raw_transcript"])
        self.assertEqual(result["raw_transcript"]["transcripts"][0]["sentences"][0]["words"][0]["text"], "Hello")
        with patch.dict(os.environ, {}, clear=True), patch.object(p, "_json_request") as network:
            self.assertEqual(self.transcribe()["status"], "completed")
        network.assert_not_called()
        self.assertNotIn("signature", (self.out / "asr-job.json").read_text())

    def test_identity_change_is_rejected_before_any_network(self):
        self.pending()
        for kwargs in ({"model": "fun-asr"}, {"language": "zh"}):
            with self.subTest(kwargs=kwargs), patch.object(p, "_json_request") as network:
                with self.assertRaisesRegex(p.ProviderError, "拒绝混用"):
                    self.transcribe(**kwargs)
                network.assert_not_called()

    def test_url_result_download_failure_does_not_create_second_task(self):
        self.pending()
        succeeded = {"output": {"task_status": "SUCCEEDED", "results": [{"subtask_status": "SUCCEEDED", "transcription_url": "https://results.example/tx"}]}}
        with patch.object(p, "_json_request", side_effect=[succeeded, p.ProviderError("网络失败")]) as network:
            result = self.transcribe()
        self.assertEqual(result["task_id"], "test-task")
        self.assertEqual(result["last_poll_error"], "network_or_response")
        self.assertTrue(all(c.kwargs.get("method", "GET") == "GET" for c in network.call_args_list))

    def test_asr_base_cannot_send_key_to_arbitrary_host(self):
        with patch.dict(os.environ, {"DASHSCOPE_HTTP_BASE_URL": "https://attacker.example/api/v1"}), patch.object(p, "_json_request") as network:
            with self.assertRaises(p.ProviderError):
                self.transcribe()
        network.assert_not_called()

    def test_transport_error_never_exposes_body_or_url(self):
        for error in (URLError("https://host/path?secret=hidden"), HTTPError("https://host/?secret=hidden", 429, "hidden", {}, io.BytesIO(b"secret-body"))):
            with self.subTest(error=type(error).__name__), patch.object(p, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(p.ProviderError) as caught:
                    p._json_request("https://service.example/api")
            self.assertNotIn("hidden", str(caught.exception))
            self.assertNotIn("secret-body", str(caught.exception))

    def test_download_restarts_partial_and_detects_short_file(self):
        dest = self.root / "video.mp4"
        dest.with_name("video.mp4.part").write_bytes(b"old-untrusted-bytes")
        with patch.object(p, "_open", return_value=Response(b"new-content")):
            digest = p._download("https://cdn.example/video", dest)
        self.assertEqual(dest.read_bytes(), b"new-content")
        self.assertEqual(digest, hashlib.sha256(b"new-content").hexdigest())
        short = self.root / "short.mp4"
        with patch.object(p, "_open", return_value=Response(b"few", headers={"Content-Length": "20"})):
            with self.assertRaisesRegex(p.ProviderError, "不完整"):
                p._download("https://cdn.example/short", short)
        self.assertFalse(short.exists())

    def test_partial_response_is_not_mistaken_for_whole_media(self):
        with patch.object(p, "_open", return_value=Response(b"part", status=206)):
            with self.assertRaisesRegex(p.ProviderError, "部分响应"):
                p._download("https://cdn.example/video", self.root / "partial.mp4")

    def test_exact_full_range_206_is_verified(self):
        dest = self.root / "full.mp4"
        with patch.object(p, "_open", return_value=Response(b"full", status=206, headers={"Content-Length": "4", "Content-Range": "bytes 0-3/4"})):
            p._download("https://cdn.example/video", dest, expected_size=4)
        self.assertEqual(dest.read_bytes(), b"full")
        with patch.object(p, "_open", return_value=Response(b"full", status=206, headers={"Content-Length": "4", "Content-Range": "bytes 0-3/40"})):
            with self.assertRaises(p.ProviderError):
                p._download("https://cdn.example/video", self.root / "short.mp4", expected_size=40)

    def ranged_response(self, data: bytes, begin: int, end: int, total: int, etag: str = '"rep-1"'):
        return Response(data, status=206, headers={"Content-Length": str(len(data)),
                        "Content-Range": f"bytes {begin}-{end}/{total}", "ETag": etag})

    def test_partial_206_downloads_exact_contiguous_ranges(self):
        requests = []
        data = b"0123456789"

        def opened(request, timeout=30):
            requests.append(request)
            if len(requests) == 1:
                self.assertIsNone(request.get_header("Range"))
                return self.ranged_response(data[:2], 0, 1, 10)
            bounds = [(2, 5), (6, 9)][len(requests) - 2]
            begin, end = bounds
            self.assertEqual(request.get_header("Range"), f"bytes={begin}-{end}")
            self.assertEqual(request.get_header("If-range"), '"rep-1"')
            return self.ranged_response(data[begin:end + 1], begin, end, 10)

        destination = self.root / "ranges.mp4"
        with patch.object(p, "CHUNK", 4), patch.object(p, "_open", side_effect=opened):
            digest = p._download("https://cdn.example/video?signature=hidden", destination, expected_size=10)
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(len(requests), 3)
        self.assertEqual({r.full_url for r in requests}, {"https://cdn.example/video?signature=hidden"})

    def test_range_download_rejects_wrong_bounds_total_and_etag(self):
        for invalid in ((3, 6, 10, '"rep-1"'), (2, 5, 11, '"rep-1"'), (2, 5, 10, '"rep-2"')):
            with self.subTest(invalid=invalid):
                responses = [self.ranged_response(b"01", 0, 1, 10), self.ranged_response(b"2345", *invalid)]
                with patch.object(p, "CHUNK", 4), patch.object(p, "_open", side_effect=responses):
                    with self.assertRaises(p.ProviderError):
                        p._download("https://cdn.example/video", self.root / "rejected.mp4", expected_size=10)
                self.assertFalse((self.root / "rejected.mp4").exists())

    def test_range_download_discovers_total_without_expected_size(self):
        with patch.object(p, "_open", side_effect=[self.ranged_response(b"01", 0, 1, 6), self.ranged_response(b"2345", 2, 5, 6)]):
            p._download("https://cdn.example/video", self.root / "discovered.mp4")
        self.assertEqual((self.root / "discovered.mp4").read_bytes(), b"012345")

    def test_range_download_rejects_size_change_and_full_response_midway(self):
        with patch.object(p, "_open", return_value=self.ranged_response(b"01", 0, 1, 10)) as network:
            with self.assertRaisesRegex(p.ProviderError, "总长度"):
                p._download("https://cdn.example/video", self.root / "wrong-size.mp4", expected_size=11)
        self.assertEqual(network.call_count, 1)
        with patch.object(p, "_open", side_effect=[self.ranged_response(b"01", 0, 1, 10), Response(b"0123456789")]):
            with self.assertRaisesRegex(p.ProviderError, "未返回 206"):
                p._download("https://cdn.example/video", self.root / "midway.mp4", expected_size=10)

    def test_interrupted_range_restarts_from_zero_even_with_same_size(self):
        destination = self.root / "resumed.mp4"
        with patch.object(p, "_open", side_effect=[self.ranged_response(b"01", 0, 1, 6), p.ProviderError("网络中断")]):
            with self.assertRaises(p.ProviderError):
                p._download("https://cdn.example/video", destination, expected_size=6)
        self.assertFalse(destination.exists())
        self.assertEqual(destination.with_name("resumed.mp4.part").read_bytes(), b"01")
        requests = []

        def opened(request, timeout=30):
            requests.append(request)
            if len(requests) == 1:
                self.assertIsNone(request.get_header("Range"))
                return self.ranged_response(b"AB", 0, 1, 6, '"new-representation"')
            self.assertEqual(request.get_header("Range"), "bytes=2-5")
            return self.ranged_response(b"CDEF", 2, 5, 6, '"new-representation"')

        with patch.object(p, "_open", side_effect=opened):
            p._download("https://cdn.example/video", destination, expected_size=6)
        self.assertEqual(destination.read_bytes(), b"ABCDEF")

    def test_finalizing_recovers_crashes_before_and_after_rename(self):
        for checkpoint in ("before_rename", "after_rename"):
            target = self.root / checkpoint
            real_save = p._save

            def crashing_save(path, state):
                if checkpoint == "before_rename" and state["status"] == "finalizing":
                    real_save(path, state)
                    raise KeyboardInterrupt
                if checkpoint == "after_rename" and state["status"] == "completed":
                    raise KeyboardInterrupt
                real_save(path, state)

            with self.subTest(checkpoint=checkpoint), patch.object(p, "_save", side_effect=crashing_save), patch.object(p, "_open", return_value=Response(b"video-content")), patch.object(p, "_media_check"):
                with self.assertRaises(KeyboardInterrupt):
                    p.fetch("https://cdn.example/video.mp4", target)
            saved = json.loads((target / "fetch.json").read_text())
            self.assertEqual(saved["status"], "finalizing")
            with patch.object(p, "_open") as network, patch.object(p, "_media_check") as check:
                completed = p.fetch("https://cdn.example/video.mp4", target)
            network.assert_not_called()
            check.assert_not_called()
            self.assertEqual(completed["status"], "completed")
            self.assertEqual((target / "source.mp4").read_bytes(), b"video-content")

    def test_media_check_requires_full_decode_after_probe(self):
        probe_result = type("Probe", (), {"stdout": json.dumps({"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}).encode()})()
        with patch.object(p.subprocess, "run", return_value=probe_result), patch("hc.media.decode_check") as decode:
            p._media_check(self.source)
        decode.assert_called_once_with(self.source)
        with patch.object(p.subprocess, "run", return_value=probe_result), patch("hc.media.decode_check", side_effect=RuntimeError("decode failure")):
            with self.assertRaisesRegex(p.ProviderError, "完整解码失败"):
                p._media_check(self.source)

    def test_auto_language_omits_hints_and_requests_one_channel(self):
        def request(url, **kwargs):
            if kwargs.get("method") == "POST":
                self.assertNotIn("language_hints", kwargs["payload"]["parameters"])
                self.assertEqual(kwargs["payload"]["parameters"]["channel_id"], [0])
                self.assertTrue(kwargs["payload"]["parameters"]["diarization_enabled"])
                return {"output": {"task_id": "test-auto"}}
            return {"output": {"task_status": "RUNNING"}}
        with patch.object(p, "_json_request", side_effect=request):
            self.assertEqual(self.transcribe(language="auto")["status"], "running")

    def test_audio_url_mismatch_blocks_submission_even_if_length_matches(self):
        with patch("hc.media.extract_audio", side_effect=self.fake_audio), patch.object(p, "_open", return_value=Response(b"x" * len(b"prepared-wave"))), patch.object(p, "_json_request") as api:
            with self.assertRaisesRegex(p.ProviderError, "不一致；未提交 ASR"):
                p.transcribe(self.source, self.out, self.sha, 10_000,
                             audio_url="https://media.example/wrong.wav?signature=hidden")
        api.assert_not_called()
        state = json.loads((self.out / "asr-job.json").read_text())
        self.assertEqual(state["status"], "new")
        self.assertEqual(state["audio_sha256"], hashlib.sha256(b"prepared-wave").hexdigest())
        self.assertEqual(state["source_sha256"], self.sha)
        self.assertNotIn("signature", json.dumps(state))
        self.assertEqual(list(self.out.glob(".asr-url-check-*")), [])

    def test_matching_audio_url_needs_no_fal_and_resumes_bound_task(self):
        with patch("hc.media.extract_audio", side_effect=self.fake_audio) as extract, patch.object(p, "_open", return_value=Response(b"prepared-wave")) as download, patch.object(p, "_upload_audio") as upload, patch.object(p, "_json_request", side_effect=[
            {"output": {"task_id": "verified-audio"}},
            {"output": {"task_status": "RUNNING"}},
            {"output": {"task_status": "RUNNING"}},
        ]) as api:
            first = p.transcribe(self.source, self.out, self.sha, 10_000,
                                 audio_url="https://media.example/exact.wav?signature=hidden")
            second = p.transcribe(self.source, self.out, self.sha, 10_000)
        upload.assert_not_called()
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(first["audio_sha256"], hashlib.sha256(b"prepared-wave").hexdigest())
        self.assertEqual(second["task_id"], "verified-audio")
        self.assertEqual(sum(c.kwargs.get("method") == "POST" for c in api.call_args_list), 1)
        self.assertNotIn("signature", (self.out / "asr-job.json").read_text())

    def test_audio_preparation_recovers_publish_before_hash_save_crash(self):
        real_save = p._save

        def crashing_save(path, state):
            if state.get("audio_sha256"):
                raise KeyboardInterrupt
            real_save(path, state)

        with patch.object(p, "_save", side_effect=crashing_save), patch("hc.media.extract_audio", side_effect=self.fake_audio), patch.object(p, "_json_request") as api:
            with self.assertRaises(KeyboardInterrupt):
                p.transcribe(self.source, self.out, self.sha, 10_000,
                             audio_url="https://media.example/audio.wav")
        api.assert_not_called()
        self.assertEqual((self.out / "asr-input.wav").read_bytes(), b"prepared-wave")
        self.assertNotIn("audio_sha256", json.loads((self.out / "asr-job.json").read_text()))
        with patch("hc.media.extract_audio", side_effect=self.fake_audio) as extract, patch.object(p, "_open", return_value=Response(b"prepared-wave")), patch.object(p, "_json_request", side_effect=[
            {"output": {"task_id": "recovered-audio"}},
            {"output": {"task_status": "PENDING"}},
        ]):
            result = p.transcribe(self.source, self.out, self.sha, 10_000,
                                  audio_url="https://media.example/audio.wav")
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(result["task_id"], "recovered-audio")
        self.assertEqual(result["audio_sha256"], hashlib.sha256(b"prepared-wave").hexdigest())

    def test_unbound_different_audio_is_preserved_and_cannot_be_submitted(self):
        self.out.mkdir()
        existing = self.out / "asr-input.wav"
        existing.write_bytes(b"different-wave")
        with patch("hc.media.extract_audio", side_effect=self.fake_audio), patch.object(p, "_json_request") as api:
            with self.assertRaisesRegex(p.ProviderError, "重新提取的内容不同"):
                p.transcribe(self.source, self.out, self.sha, 10_000,
                             audio_url="https://media.example/audio.wav")
        self.assertEqual(existing.read_bytes(), b"different-wave")
        api.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本机 FFmpeg")
    def test_real_wav_extraction_can_recover_missing_hash(self):
        audio_source = self.root / "real-source.wav"
        with wave.open(str(audio_source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16000)
        self.out.mkdir()
        original = {"status": "new", "source_sha256": p._sha(audio_source)}
        state = dict(original)
        job_path = self.out / "asr-job.json"
        audio = p._prepare_audio(audio_source, self.out, state, job_path)
        first_hash = p._sha(audio)
        # 模拟媒体已发布，但 job 中的 audio_sha256 尚未持久化。
        p._save(job_path, original)
        recovered = dict(original)
        p._prepare_audio(audio_source, self.out, recovered, job_path)
        self.assertEqual(recovered["audio_sha256"], first_hash)
        self.assertEqual(p._sha(audio), first_hash)

    def test_completed_fetch_reuses_hash_checked_cache_without_network(self):
        with patch.object(p, "_open", return_value=Response(b"video-content")), patch.object(p, "_media_check"):
            result = p.fetch("https://cdn.example/video.mp4?signature=hidden", self.out)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("signature", json.dumps(result))
        with patch.object(p, "_open") as network:
            p.fetch("https://cdn.example/video.mp4?signature=hidden", self.out)
        network.assert_not_called()
        (self.out / "source.mp4").write_bytes(b"edited")
        with patch.object(p, "_open") as network:
            with self.assertRaisesRegex(p.ProviderError, "哈希变化"):
                p.fetch("https://cdn.example/video.mp4?signature=hidden", self.out)
        network.assert_not_called()

    def test_fetch_different_source_never_reuses_same_length(self):
        with patch.object(p, "_open", return_value=Response(b"video-content")), patch.object(p, "_media_check"):
            p.fetch("https://cdn.example/a.mp4", self.out)
        with patch.object(p, "_open") as network:
            with self.assertRaisesRegex(p.ProviderError, "另一条来源"):
                p.fetch("https://cdn.example/b.mp4", self.out)
        network.assert_not_called()

    def test_youtube_highest_quality_original_audio_matching_subtitle(self):
        post = {"medias": [
            {"media_type": "video", "variants": [{"quality": 720, "video_url": "https://cdn.example/720"},
              {"quality": 2160, "video_url": "https://cdn.example/2160"}, {"quality": 1080, "video_url": "https://cdn.example/1080"}],
             "subtitles": [{"language_tag": "en", "urls": [{"format": "srt", "url": "https://cdn.example/en.srt"}]},
                           {"language_tag": "zh", "urls": [{"format": "srt", "url": "https://cdn.example/zh.srt"}]}]},
            {"media_type": "audio", "variants": [{"is_default": True, "language_tag": "zh", "audio_url": "https://cdn.example/dub"},
             {"quality_label": "Original", "language_tag": "en", "audio_url": "https://cdn.example/original"}]}]}
        video, picked, audio = p._pick_media(post)
        self.assertEqual(picked["quality"], 2160)
        self.assertEqual(audio["audio_url"], "https://cdn.example/original")
        self.assertEqual(p._subtitle(video, picked, audio), ("https://cdn.example/en.srt", "en"))

    def test_youtube_identity_mismatch_never_downloads(self):
        with patch.object(p, "_json_request", return_value={"site": "youtube", "id": "xxxxxxxxxxx"}), patch.object(p, "_download") as download:
            with self.assertRaisesRegex(p.ProviderError, "身份"):
                p.fetch("https://www.youtube.com/watch?v=UmAfYygkQ6A", self.out)
        download.assert_not_called()

    def test_fal_upload_streams_put_without_forwarding_key(self):
        audio = self.root / "input.wav"
        audio.write_bytes(b"wavdata")
        captured = []

        def opened(request, timeout=30):
            captured.append(request)
            self.assertEqual(request.data.read(), b"wavdata")
            return Response(b"", status=200)

        with patch.dict(os.environ, {"FAL_KEY": "fal-secret"}), patch.object(p, "_json_request", return_value={"upload_url": "https://storage.example/put?signature=hidden", "file_url": "https://storage.example/audio.wav"}) as init, patch.object(p, "_open", side_effect=opened):
            self.assertEqual(p._upload_audio(audio), "https://storage.example/audio.wav")
        self.assertEqual(init.call_args.kwargs["headers"]["Authorization"], "Key fal-secret")
        self.assertFalse(captured[0].has_header("Authorization"))
        self.assertEqual(captured[0].get_method(), "PUT")

    def test_credentials_not_forwarded_on_redirect(self):
        request = Request("https://api.example/submit", headers={"Authorization": "Bearer secret"})
        with self.assertRaisesRegex(p.ProviderError, "重定向"):
            p._SafeRedirect().redirect_request(request, None, 302, "redirect", {}, "https://other.example/target")


if __name__ == "__main__":
    unittest.main()
