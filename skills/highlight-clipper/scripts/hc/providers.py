"""外部下载与 ASR 适配；密钥仅从本次进程的环境变量读取。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SNAPANY_URL = "https://api.snapany.com/openapi/v1/extract/post"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_ASR_MODEL = "qwen-audio-3.1-asr-flash-filetrans"
FAL_UPLOAD_URL = "https://rest.fal.ai/storage/upload/initiate?storage_type=gcs"
CHUNK = 1024 * 1024


class ProviderError(RuntimeError):
    """只包含可公开的错误类别，不拼接响应正文、签名 URL 或底层异常。"""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _key(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ProviderError(f"当前进程缺少环境变量 {name}")
    return value


def _https(url: str) -> str:
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname and
                 parts.username is None and parts.password is None and
                 not any(ord(c) < 32 for c in url))
        _ = parts.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProviderError("需要不含用户名和密码的有效 HTTPS 地址")
    return url


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https(newurl)
        # 已鉴权 API 不跟随重定向，避免将凭据发送到另一个来源。
        if req.has_header("Authorization"):
            raise ProviderError("鉴权接口发生重定向，已停止请求")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            for name in ("Cookie", "Authorization", "Proxy-authorization"):
                new.remove_header(name)
        return new


def _open(request: Request, timeout: float = 30):
    _https(request.full_url)
    try:
        return build_opener(_SafeRedirect()).open(request, timeout=timeout)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise ProviderError(f"外部服务 HTTP {code}", status_code=code) from None
    except (URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
        raise ProviderError("外部服务网络请求失败") from None


def _json_request(url: str, *, method: str = "GET", headers: dict | None = None,
                  payload: dict | None = None, timeout: float = 30) -> dict:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    try:
        with _open(Request(url, data=data, headers=request_headers, method=method), timeout) as response:
            raw = response.read(32 * CHUNK + 1)
            if len(raw) > 32 * CHUNK:
                raise ProviderError("外部服务 JSON 超过大小上限")
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        raise ProviderError("外部服务返回无效 JSON") from None
    except (OSError, http.client.HTTPException):
        raise ProviderError("外部服务响应读取失败") from None
    if not isinstance(body, dict):
        raise ProviderError("外部服务 JSON 根节点必须是对象")
    return body


def _save(path: Path, body: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as stream:
        json.dump(body, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ProviderError("本地服务状态文件损坏；请保留文件后检查") from None
    if not isinstance(data, dict):
        raise ProviderError("本地服务状态文件格式错误")
    return data


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _public_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _download(url: str, destination: Path, *, headers: dict | None = None,
              expected_size: int | None = None, max_bytes: int | None = None) -> str:
    """验证完整响应或连续 Range；跨次调用从零重下当前临时文件。"""
    _https(url)
    if destination.exists():
        raise ProviderError("目标下载文件已存在，拒绝覆盖")
    temporary = destination.with_name(destination.name + ".part")
    request_headers = {**(headers or {}), "Accept-Encoding": "identity"}
    request = Request(url, headers=request_headers)
    total = 0
    digest = hashlib.sha256()

    def byte_range(response) -> tuple[int, int, int]:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
        if not match:
            raise ProviderError("部分响应缺少有效 Content-Range")
        begin, end, size = map(int, match.groups())
        if not 0 <= begin <= end < size:
            raise ProviderError("部分响应的范围无效")
        return begin, end, size

    def copy_response(response, stream, length: int | None) -> None:
        nonlocal total
        header_length = response.headers.get("Content-Length")
        declared = int(header_length) if header_length is not None else None
        if declared is not None and (declared < 0 or (length is not None and declared != length)):
            raise ProviderError("下载响应长度与请求范围不一致")
        received = 0
        while block := response.read(CHUNK):
            received += len(block)
            total += len(block)
            if length is not None and received > length:
                raise ProviderError("下载响应超过请求范围")
            if max_bytes is not None and total > max_bytes:
                raise ProviderError("下载内容超过允许大小")
            if expected_size is not None and total > expected_size:
                raise ProviderError("下载内容超过媒体规格")
            digest.update(block)
            stream.write(block)
        if received == 0 or (length is not None and received != length) or (declared is not None and received != declared):
            raise ProviderError("文件下载不完整；下次将重新下载当前临时文件")

    try:
        # 明确截断旧 .part；不把未验证的上一次内容当作已完成字节。
        with temporary.open("wb") as stream:
            with _open(request) as response:
                range_size = None
                etag = None
                if response.status == 206:
                    begin, end, range_size = byte_range(response)
                    if begin != 0:
                        raise ProviderError("首次部分响应未从零字节开始")
                    if expected_size is not None and range_size != expected_size:
                        raise ProviderError("下载范围总长度与媒体规格不一致")
                    if max_bytes is not None and range_size > max_bytes:
                        raise ProviderError("下载内容超过允许大小")
                    raw_etag = response.headers.get("ETag", "")
                    if re.fullmatch(r'"[^"\r\n]+"', raw_etag):
                        etag = raw_etag
                    copy_response(response, stream, end + 1)
                elif response.status == 200:
                    copy_response(response, stream, expected_size)
                else:
                    raise ProviderError("下载响应不是可用文件")
            while range_size is not None and total < range_size:
                begin = total
                end = min(begin + CHUNK - 1, range_size - 1)
                range_headers = {**request_headers, "Range": f"bytes={begin}-{end}"}
                if etag:
                    range_headers["If-Range"] = etag
                # 始终使用本次获取的同一个 URL；不混入刷新后的媒体链接。
                with _open(Request(url, headers=range_headers)) as response:
                    if response.status != 206:
                        raise ProviderError("Range 请求未返回 206，可能媒体规格已变化")
                    actual_begin, actual_end, actual_size = byte_range(response)
                    if (actual_begin, actual_end, actual_size) != (begin, end, range_size):
                        raise ProviderError("Range 响应起止或总长度与请求不一致")
                    if etag and response.headers.get("ETag") not in (None, etag):
                        raise ProviderError("Range 响应 ETag 变化，拒绝拼接不同媒体")
                    copy_response(response, stream, end - begin + 1)
            if range_size is not None and total != range_size:
                raise ProviderError("下载范围总长度与已接收内容不一致")
            if expected_size is not None and total != expected_size:
                raise ProviderError("下载范围总长度与媒体规格不一致")
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.stat().st_size != total or _sha(temporary) != digest.hexdigest():
            raise ProviderError("下载临时文件的长度或哈希验证失败")
        temporary.replace(destination)
    except ProviderError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise ProviderError("下载中断；下次将重新下载当前临时文件") from None
    return digest.hexdigest()


def _media_check(path: Path) -> None:
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                                capture_output=True, check=True)
        types = {s.get("codec_type") for s in json.loads(result.stdout).get("streams", [])}
        if not {"video", "audio"} <= types:
            raise ProviderError("下载结果需要同时包含视频和音频轨")
        from .media import decode_check
        decode_check(path)
    except ProviderError:
        raise
    except (OSError, subprocess.CalledProcessError, ValueError):
        raise ProviderError("无法检查下载媒体；请确认 ffprobe 可用且媒体有效") from None
    except RuntimeError:
        raise ProviderError("下载媒体完整解码失败；请检查媒体内容和 FFmpeg") from None


def _youtube_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    video_id = None
    if host == "youtu.be":
        video_id = parts.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parts.path == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
        elif parts.path.startswith(("/shorts/", "/live/", "/embed/")):
            video_id = parts.path.split("/")[2]
        if not video_id:
            raise ProviderError("需要单条 YouTube 视频地址")
    if video_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ProviderError("YouTube 视频 ID 格式不正确")
    return video_id


def _size(value: Any) -> int | None:
    if value is None:
        return None
    try:
        size = int(value)
        if size <= 0:
            raise ValueError
        return size
    except (ValueError, TypeError):
        raise ProviderError("媒体规格的文件大小无效") from None


def _pick_media(post: dict) -> tuple[dict, dict, dict | None]:
    """选择最高可用视频规格，沿用 video-fetcher 的 Original 原声音轨规则。"""
    medias = post.get("medias", [])
    if not isinstance(medias, list):
        raise ProviderError("SnapAny 缺少媒体列表")
    video = next((m for m in medias if isinstance(m, dict) and m.get("media_type") == "video"), None)
    if video is None:
        raise ProviderError("SnapAny 未返回视频")
    variants = [v for v in video.get("variants", []) if isinstance(v, dict) and v.get("video_url")]
    scored = []
    for variant in variants:
        try:
            scored.append((int(variant.get("quality")), variant))
        except (TypeError, ValueError):
            continue
    if scored:
        picked = max(scored, key=lambda v: v[0])[1]
    elif video.get("resource_url"):
        picked = {"video_url": video["resource_url"]}
    else:
        raise ProviderError("SnapAny 未提供可用视频规格")
    audio_media = next((m for m in medias if isinstance(m, dict) and m.get("media_type") == "audio"), {})
    audio_variants = [v for v in audio_media.get("variants", []) if isinstance(v, dict) and v.get("audio_url")]
    original = [v for v in audio_variants if str(v.get("quality_label", "")).lower() in {"original", "origianl"}
                or "acont=original" in unquote(str(v.get("audio_url", ""))).lower()]
    default = [v for v in audio_variants if v.get("is_default") is True]
    audio = (original or default or audio_variants or ([picked] if picked.get("audio_url") else [None]))[0]
    if audio is not None:
        audio = {**audio, "headers": audio_media.get("headers", video.get("headers", {}))}
    return video, picked, audio


def _subtitle(video: dict, picked: dict, audio: dict | None) -> tuple[str, str] | None:
    tag = (audio or {}).get("language_tag") or picked.get("language_tag")
    if not isinstance(tag, str) or not tag:
        return None
    options = [s for s in video.get("subtitles", []) if isinstance(s, dict)]
    exact = [s for s in options if str(s.get("language_tag", "")).lower() == tag.lower()]
    primary = [s for s in options if str(s.get("language_tag", "")).lower().split("-")[0] == tag.lower().split("-")[0]]
    for entry in exact or primary:
        for item in entry.get("urls", []):
            if isinstance(item, dict) and item.get("format") == "srt" and item.get("url"):
                return item["url"], entry["language_tag"]
    return None


def _media_headers(raw: Any) -> dict[str, str]:
    # 不将第三方媒体元数据中的鉴权头转发给 CDN。
    allowed = {"user-agent", "referer", "origin", "accept", "accept-language"}
    return {str(k): str(v) for k, v in raw.items() if str(k).lower() in allowed} if isinstance(raw, dict) else {}


def fetch(url: str, output_dir: Path) -> dict:
    """下载 YouTube（SnapAny）或 HTTPS 直链；成功源按 URL 身份和哈希缓存。"""
    _https(url)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    video_id = _youtube_id(url)
    canonical = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    identity = _url_hash(canonical)
    manifest_path = output_dir / "fetch.json"
    if manifest_path.exists():
        previous = _read(manifest_path)
        if previous.get("url_sha256") != identity:
            raise ProviderError("下载目录属于另一条来源；请使用新的项目目录")
        if previous.get("status") in {"completed", "finalizing"}:
            video_path = output_dir / "source.mp4"
            candidate = video_path
            if previous["status"] == "finalizing" and not video_path.exists():
                candidate = output_dir / ".fetch-work" / "merged.mp4"
            if not candidate.is_file() or _sha(candidate) != previous.get("sha256"):
                raise ProviderError("已完成源文件缺失或哈希变化；拒绝使用不一致缓存")
            if previous.get("subtitle_path"):
                subtitle_path = output_dir / "source.srt"
                if not subtitle_path.is_file() or _sha(subtitle_path) != previous.get("subtitle_sha256"):
                    raise ProviderError("字幕缓存缺失或哈希变化")
                previous["subtitle_path"] = str(subtitle_path)
            if not previous.get("decode_verified"):
                _media_check(candidate)
            if candidate != video_path:
                candidate.replace(video_path)
            previous.update(status="completed", video_path=str(video_path), decode_verified=True)
            _save(manifest_path, previous)
            return previous
    final = output_dir / "source.mp4"
    if final.exists():
        raise ProviderError("发现未登记的完成源文件；请检查，拒绝覆盖")
    state = {"status": "downloading", "url_sha256": identity,
             "source_url": canonical if video_id else _public_url(url), "title": "",
             "subtitle_path": None}
    _save(manifest_path, state)
    work = output_dir / ".fetch-work"
    work.mkdir(exist_ok=True)
    # 此目录只保存未交付中间轨道；每次重新请求得到的规格都从零下载。
    for name in ("video.bin", "audio.bin", "merged.mp4", "subtitles.srt"):
        (work / name).unlink(missing_ok=True)
    if video_id:
        body = _json_request(SNAPANY_URL, method="POST", headers={"Authorization": f"Bearer {_key('SNAPANY_API_KEY')}",
                             "Accept-Language": "zh"}, payload={"url": canonical}, timeout=60)
        if body.get("site") != "youtube" or str(body.get("id")) != video_id:
            raise ProviderError("SnapAny 返回的视频身份与请求不一致")
        video, picked, audio = _pick_media(body)
        state["title"] = str(body.get("title") or "")
        _download(picked["video_url"], work / "video.bin", headers=_media_headers(video.get("headers")),
                  expected_size=_size(picked.get("video_filesize")))
        if audio:
            _download(audio["audio_url"], work / "audio.bin", headers=_media_headers(audio.get("headers")),
                      expected_size=_size(audio.get("audio_filesize")))
        subtitle = _subtitle(video, picked, audio)
        if subtitle:
            try:
                _download(subtitle[0], work / "subtitles.srt", headers=_media_headers(video.get("headers")), max_bytes=16 * CHUNK)
                subtitle_text = (work / "subtitles.srt").read_text(encoding="utf-8-sig")
                if "-->" not in subtitle_text or "<html" in subtitle_text.lower():
                    raise ProviderError("字幕内容不是 SRT")
                (work / "subtitles.srt").replace(output_dir / "source.srt")
                state.update(subtitle_path=str(output_dir / "source.srt"), subtitle_language=subtitle[1],
                             subtitle_sha256=_sha(output_dir / "source.srt"))
            except (ProviderError, UnicodeError):
                state["subtitle_status"] = "unavailable"
        command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(work / "video.bin")]
        if audio:
            command += ["-i", str(work / "audio.bin"), "-map", "0:v:0", "-map", "1:a:0"]
        else:
            command += ["-map", "0:v:0", "-map", "0:a:0"]
        command += ["-c", "copy", "-movflags", "+faststart", str(work / "merged.mp4")]
        try:
            subprocess.run(command, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            raise ProviderError("FFmpeg 合并失败；请检查工具和音视频格式") from None
    else:
        _download(url, work / "merged.mp4")
    _media_check(work / "merged.mp4")
    sha = _sha(work / "merged.mp4")
    # 原子发布前保存来源、哈希及验证结果；rename 前后中断都能恢复。
    state.update(status="finalizing", video_path=str(final), sha256=sha, decode_verified=True)
    _save(manifest_path, state)
    (work / "merged.mp4").replace(final)
    state["status"] = "completed"
    _save(manifest_path, state)
    return state


def _upload_audio(path: Path) -> str:
    """fal 官方 SDK 的 storage_type=gcs 两阶段上传协议；流式 PUT。"""
    result = _json_request(FAL_UPLOAD_URL, method="POST", headers={"Authorization": f"Key {_key('FAL_KEY')}"},
                           payload={"file_name": "highlight-clipper-audio.wav", "content_type": "audio/wav"})
    upload_url, file_url = result.get("upload_url"), result.get("file_url")
    if not isinstance(upload_url, str) or not isinstance(file_url, str):
        raise ProviderError("fal 未返回可用上传地址")
    _https(upload_url)
    _https(file_url)
    try:
        with path.open("rb") as stream:
            request = Request(upload_url, data=stream, method="PUT", headers={"Content-Type": "audio/wav",
                              "Content-Length": str(path.stat().st_size)})
            with _open(request, timeout=60) as response:
                if not 200 <= response.status < 300:
                    raise ProviderError("fal 文件上传未成功")
    except (OSError, http.client.HTTPException):
        raise ProviderError("fal 文件上传中断") from None
    return file_url


def _dashscope_base() -> str:
    base = os.environ.get("DASHSCOPE_HTTP_BASE_URL", DASHSCOPE_BASE).strip().rstrip("/")
    _https(base)
    parts = urlsplit(base)
    host = parts.hostname or ""
    permitted = host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} or bool(
        re.fullmatch(r"[A-Za-z0-9-]+\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com", host))
    if not permitted or parts.path != "/api/v1" or parts.query or parts.fragment or parts.port not in (None, 443):
        raise ProviderError("DASHSCOPE_HTTP_BASE_URL 必须是官方百炼 /api/v1 地址")
    return base


def _safe_result(raw: dict) -> dict:
    """保留转写内容和时间轴，去掉服务端回显的签名 URL 字段。"""
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k.lower() != "url" and not k.lower().endswith("_url")}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return clean(raw)


def _prepare_audio(source: Path, output_dir: Path, job: dict, job_path: Path) -> Path:
    """音频绑定源身份；提取完成与哈希落盘之间的中断通过重新提取核验恢复。"""
    from .media import extract_audio

    audio = output_dir / "asr-input.wav"
    known_hash = job.get("audio_sha256")
    if audio.exists() and known_hash:
        if _sha(audio) != known_hash:
            raise ProviderError("已有转写音频的哈希变化；拒绝使用不一致缓存")
        return audio
    # 先记录源文件身份；媒体提取和提交 ASR 属于不同阶段。
    _save(job_path, job)
    with tempfile.TemporaryDirectory(prefix=".asr-prepare-", dir=output_dir) as temporary_dir:
        prepared = extract_audio(source, Path(temporary_dir) / "asr-input.wav")
        digest = _sha(prepared)
        if known_hash and digest != known_hash:
            raise ProviderError("重新提取的转写音频与已记录哈希不一致")
        if audio.exists():
            if _sha(audio) != digest:
                raise ProviderError("已有转写音频与源视频重新提取的内容不同；已保留现有文件")
        else:
            prepared.replace(audio)
        job["audio_sha256"] = digest
        _save(job_path, job)
    return audio


def _verify_audio_url(audio_url: str, audio: Path, output_dir: Path) -> None:
    """外部 URL 必须托管本工具从同一源提取的 WAV；不接受仅凭声明的对应关系。"""
    _https(audio_url)
    with tempfile.TemporaryDirectory(prefix=".asr-url-check-", dir=output_dir) as temporary_dir:
        remote = Path(temporary_dir) / "remote.wav"
        digest = _download(audio_url, remote, expected_size=audio.stat().st_size)
        if digest != _sha(audio):
            raise ProviderError("audio_url 的文件与本项目 asr-input.wav 不一致；未提交 ASR")


def transcribe(source: Path, output_dir: Path, source_sha256: str, duration_ms: int,
               language: str = "en", model: str | None = None, audio_url: str | None = None,
               poll_seconds: float = 0) -> dict:
    """提交一次、持久保存任务并恢复轮询；未知提交绝不自动重复付费请求。"""
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha256) or duration_ms <= 0:
        raise ProviderError("转写需要有效源哈希和时长")
    if not 0 <= poll_seconds <= 60:
        raise ProviderError("单次轮询等待必须介于 0 到 60 秒")
    model = model or DEFAULT_ASR_MODEL
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ProviderError("ASR 模型名称无效")
    if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", language):
        raise ProviderError("转写语言应为 en、zh 或 auto 等语言标记")
    base = _dashscope_base()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    job_path = output_dir / "asr-job.json"
    identity = {"source_sha256": source_sha256, "source_duration_ms": duration_ms,
                "model": model, "language": language, "api_base": base}
    job = _read(job_path) if job_path.exists() else {**identity, "status": "new"}
    if any(job.get(k) != v for k, v in identity.items()):
        raise ProviderError("已有 ASR 任务的源文件、模型、语言或服务地址不同；拒绝混用")
    if job.get("status") == "completed":
        if not isinstance(job.get("raw_transcript"), dict):
            raise ProviderError("ASR 成功缓存缺少转写结果")
        return job
    if job.get("status") in {"failed", "cancelled", "submit_rejected", "submission_unknown", "submitting"}:
        return job
    api_key = _key("DASHSCOPE_API_KEY")
    auth = {"Authorization": f"Bearer {api_key}"}
    if not job.get("task_id"):
        if job.get("status") != "new":
            raise ProviderError("ASR 状态缺少任务 ID；必须人工核实，不能重新提交")
        if not source.is_file() or _sha(source) != source_sha256:
            raise ProviderError("源视频与转写身份哈希不一致")
        if audio_url is None:
            _key("FAL_KEY")
        else:
            _https(audio_url)
        audio = _prepare_audio(source, output_dir, job, job_path)
        if audio_url is not None:
            _verify_audio_url(audio_url, audio, output_dir)
        else:
            audio_url = _upload_audio(audio)
        parameters = {"diarization_enabled": True, "channel_id": [0]}
        if language != "auto":
            parameters["language_hints"] = [language.split("-")[0].lower()]
        payload = {"model": model, "input": {"file_urls": [audio_url]}, "parameters": parameters}
        # 必须在 HTTP POST 之前落盘；进程被杀时下次也不会重交。
        job["status"] = "submitting"
        _save(job_path, job)
        try:
            response = _json_request(base + "/services/audio/asr/transcription", method="POST",
                                     headers={**auth, "X-DashScope-Async": "enable"}, payload=payload)
            output = response.get("output")
            task_id = output.get("task_id") if isinstance(output, dict) else None
            if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
                raise ProviderError("ASR 提交响应缺少有效任务 ID")
        except ProviderError as exc:
            job["status"] = "submit_rejected" if exc.status_code is not None and 400 <= exc.status_code < 500 else "submission_unknown"
            if exc.status_code is not None:
                job["http_status"] = exc.status_code
            _save(job_path, job)
            return job
        job.update(task_id=task_id, status="pending")
        _save(job_path, job)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(job["task_id"])):
        raise ProviderError("已保存的 ASR 任务 ID 无效")
    deadline = time.monotonic() + poll_seconds
    while True:
        timeout = min(30, max(1, deadline - time.monotonic())) if poll_seconds else 30
        try:
            body = _json_request(base + "/tasks/" + job["task_id"], headers=auth, timeout=timeout)
            output = body.get("output")
            if not isinstance(output, dict):
                raise ProviderError("ASR 查询响应缺少 output")
            status = output.get("task_status")
            if isinstance(body.get("usage"), dict):
                job["usage"] = _safe_result(body["usage"])
            if status in {"FAILED", "CANCELED", "CANCELLED"}:
                job["status"] = "failed" if status == "FAILED" else "cancelled"
                _save(job_path, job)
                return job
            if status == "SUCCEEDED":
                results = output.get("results", [])
                if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
                    raise ProviderError("ASR 结果应包含一个文件")
                if results[0].get("subtask_status") != "SUCCEEDED":
                    job["status"] = "failed"
                    _save(job_path, job)
                    return job
                result_url = results[0].get("transcription_url")
                if not isinstance(result_url, str):
                    raise ProviderError("ASR 成功响应缺少转写文件地址")
                _https(result_url)
                raw = _json_request(result_url, timeout=timeout)
                if not isinstance(raw.get("transcripts"), list) or not raw["transcripts"]:
                    raise ProviderError("ASR 转写结果缺少 transcripts")
                job.update(status="completed", raw_transcript=_safe_result(raw))
                job.pop("last_poll_error", None)
                _save(job_path, job)
                return job
            job["status"] = "running" if status == "RUNNING" else "pending"
            job.pop("last_poll_error", None)
        except ProviderError as exc:
            job["last_poll_error"] = "http_" + str(exc.status_code) if exc.status_code else "network_or_response"
        _save(job_path, job)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return job
        time.sleep(min(5, remaining))
        if time.monotonic() >= deadline:
            return job
