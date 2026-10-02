"""使用本地 FFmpeg 处理媒体；完成验证后才发布输出文件。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def sha256(path: Path) -> str:
    """流式计算文件指纹，不把视频一次性读入内存。"""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
    except OSError:
        raise ValueError("无法读取本地媒体文件以计算指纹。") from None
    return digest.hexdigest()


def _source(path: Path) -> Path:
    try:
        result = Path(path).expanduser().resolve(strict=True)
        if not result.is_file():
            raise ValueError
        return result
    except (OSError, ValueError, TypeError):
        raise ValueError("媒体输入必须是存在的本地文件。") from None


def _run(arguments: list[str], operation: str) -> bytes:
    # FFmpeg 的诊断可能包含 URL、路径或签名参数，因此不透传 stderr。
    try:
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False)
    except FileNotFoundError:
        raise RuntimeError(f"{operation}失败：请先安装 ffmpeg 和 ffprobe，并确保命令在 PATH 中。") from None
    except OSError:
        raise RuntimeError(f"{operation}失败：无法启动媒体处理程序。") from None
    if result.returncode:
        raise RuntimeError(f"{operation}失败：媒体处理程序退出码 {result.returncode}；请检查输入文件及编解码支持。")
    return result.stdout


def _milliseconds(value: object) -> int:
    try:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError
        return round(numeric * 1000)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("媒体包含无效的时长或起始时间。") from None


def probe(path: Path) -> dict:
    """读取媒体元信息；拒绝偏离零点超过 100 毫秒的容器时间轴。"""
    source = _source(path)
    output = _run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)
    ], "读取媒体信息")
    try:
        info = json.loads(output)
        streams = info["streams"]
        container = info["format"]
        if not isinstance(streams, list) or not isinstance(container, dict):
            raise ValueError
        video = next((s for s in streams if s.get("codec_type") == "video"
                      and not s.get("disposition", {}).get("attached_pic")), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        duration_ms = _milliseconds(container["duration"])
        start_ms = _milliseconds(container.get("start_time", 0))
        width = int(video["width"]) if video else 0
        height = int(video["height"]) if video else 0
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("无法从媒体中读取有效的时长、流类型和画面尺寸。") from None
    if duration_ms <= 0 or (video and (width <= 0 or height <= 0)) or not (video or audio):
        raise ValueError("媒体没有有效的音视频轨道或时长。")
    if abs(start_ms) > 100:
        raise ValueError("媒体时间原点偏离零点超过 100 毫秒；请先显式校准时间轴再导入。")
    return {"duration_ms": duration_ms, "width": width, "height": height,
            "has_audio": audio is not None, "has_video": video is not None,
            "start_ms": start_ms}


def decode_check(path: Path) -> None:
    """完整解码全部音视频流，损坏的包或解码错误会使检查失败。"""
    source = _source(path)
    _run(["ffmpeg", "-v", "error", "-nostdin", "-xerror", "-err_detect", "explode",
          "-i", str(source), "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"], "完整解码检查")


@contextmanager
def _temporary_output(destination: Path, suffixes: set[str]) -> Iterator[tuple[Path, Path]]:
    destination = Path(destination).expanduser().absolute()
    if destination.suffix.lower() not in suffixes:
        raise ValueError("输出文件扩展名必须是：" + "、".join(sorted(suffixes)))
    if os.path.lexists(destination):
        raise FileExistsError("目标文件已存在；请使用新的输出路径，不会覆盖已有文件。")
    temporary: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix=".hc-", suffix=destination.suffix,
                                              dir=destination.parent)
        os.close(descriptor)
        temporary = Path(filename)
        yield temporary, destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _publish(temporary: Path, destination: Path) -> None:
    # 硬链接在同一目录内原子创建，并且目标已存在时失败，避免检查后覆盖的竞争。
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise FileExistsError("目标文件在处理期间已创建；已保留原文件，请更换输出路径。") from None
    except OSError:
        raise RuntimeError("无法原子保存结果；请使用支持硬链接的本地文件系统。") from None


def _interval(start_ms: int, end_ms: int, duration_ms: int) -> None:
    if (type(start_ms) is not int or type(end_ms) is not int
            or not 0 <= start_ms < end_ms <= duration_ms):
        raise ValueError("切片时间必须是整数毫秒，且满足 0 ≤ 开始 < 结束 ≤ 原视频时长。")


def cut(source: Path, destination: Path, start_ms: int, end_ms: int) -> dict:
    """按媒体时间切出 MP4；重新编码，不依赖关键帧复制产生的近似边界。"""
    source = _source(source)
    info = probe(source)
    _interval(start_ms, end_ms, info["duration_ms"])
    if not info["has_audio"] or not info["has_video"]:
        raise ValueError("高光切片需要同时包含画面和音频的源文件。")
    with _temporary_output(destination, {".mp4"}) as (temporary, target):
        _run([
            # 输入侧 seek 配合重新编码和默认 accurate_seek，避免每段都从长视频开头解码。
            "ffmpeg", "-v", "error", "-nostdin", "-xerror", "-y", "-ss", f"{start_ms / 1000:.3f}",
            "-i", str(source), "-t", f"{(end_ms - start_ms) / 1000:.3f}",
            "-map", "0:V:0", "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "4",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(temporary)
        ], "视频切片")
        actual = probe(temporary)
        if not actual["has_audio"] or not actual["has_video"]:
            raise ValueError("切片结果缺少画面或音频。")
        if abs(actual["duration_ms"] - (end_ms - start_ms)) > 100:
            raise ValueError("切片结果时长与请求相差超过 100 毫秒，未发布结果。")
        decode_check(temporary)
        result = {"duration_ms": actual["duration_ms"], "width": actual["width"],
                  "height": actual["height"], "sha256": sha256(temporary),
                  "bytes": temporary.stat().st_size}
        _publish(temporary, target)
    return result


def extract_audio(source: Path, destination: Path) -> Path:
    """抽取第一音轨，生成 16 kHz、单声道、16 位 PCM WAV。"""
    source = _source(source)
    info = probe(source)
    if not info["has_audio"]:
        raise ValueError("源媒体没有可转写的音轨。")
    with _temporary_output(destination, {".wav"}) as (temporary, target):
        _run([
            "ffmpeg", "-v", "error", "-nostdin", "-xerror", "-y", "-i", str(source),
            "-map", "0:a:0", "-vn", "-af", "aresample=async=1:first_pts=0",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(temporary)
        ], "抽取转写音频")
        actual = probe(temporary)
        if abs(actual["duration_ms"] - info["duration_ms"]) > 100:
            raise ValueError("抽取音频与原媒体时长相差超过 100 毫秒，请检查音视频时间轴。")
        decode_check(temporary)
        _publish(temporary, target)
    return target


def frame(source: Path, destination: Path, at_ms: int) -> Path:
    """提取指定位置的一帧，供核对说话人和画面上下文。"""
    source = _source(source)
    info = probe(source)
    if not info["has_video"]:
        raise ValueError("源媒体没有可提取的画面。")
    if type(at_ms) is not int or not 0 <= at_ms < info["duration_ms"]:
        raise ValueError("截图时间必须是原视频时长内的整数毫秒。")
    with _temporary_output(destination, {".jpg", ".jpeg", ".png"}) as (temporary, target):
        _run(["ffmpeg", "-v", "error", "-nostdin", "-xerror", "-y", "-i", str(source),
              "-ss", f"{at_ms / 1000:.3f}", "-map", "0:V:0", "-frames:v", "1",
              "-update", "1", str(temporary)], "提取核对画面")
        if temporary.stat().st_size == 0:
            raise ValueError("指定时间没有可用画面，未生成截图。")
        decode_check(temporary)
        _publish(temporary, target)
    return target


def concat(sources: list[Path], destination: Path) -> dict:
    """按明确给定的顺序生成集锦；适用于本工具从同源视频导出的片段。"""
    if not isinstance(sources, list) or not sources:
        raise ValueError("集锦至少需要一个按播放顺序排列的片段。")
    inputs = [_source(source) for source in sources]
    infos = [probe(source) for source in inputs]
    if any(not info["has_audio"] or not info["has_video"] for info in infos):
        raise ValueError("集锦的每个片段都必须包含画面和音频。")
    if len({(info["width"], info["height"]) for info in infos}) != 1:
        raise ValueError("集锦片段画面尺寸不一致；请先从同一素材生成统一尺寸的片段。")
    expected_ms = sum(info["duration_ms"] for info in infos)
    arguments = ["ffmpeg", "-v", "error", "-nostdin", "-xerror", "-y"]
    filters = []
    labels = []
    for index, (source, info) in enumerate(zip(inputs, infos)):
        arguments.extend(["-i", str(source)])
        seconds = f"{info['duration_ms'] / 1000:.3f}"
        filters.append(f"[{index}:V:0]trim=duration={seconds},setpts=PTS-STARTPTS[v{index}]")
        filters.append(f"[{index}:a:0]atrim=duration={seconds},asetpts=PTS-STARTPTS[a{index}]")
        labels.append(f"[v{index}][a{index}]")
    filters.append("".join(labels) + f"concat=n={len(inputs)}:v=1:a=1[video][audio]")
    # 路径以独立 argv 传入，不通过 shell，也不插入 FFmpeg filter 或 concat 清单。
    arguments.extend(["-filter_complex", ";".join(filters), "-map", "[video]", "-map", "[audio]",
                      "-map_metadata", "-1", "-map_chapters", "-1", "-c:v", "libx264",
                      "-preset", "veryfast", "-crf", "20", "-threads", "4",
                      "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"])
    with _temporary_output(destination, {".mp4"}) as (temporary, target):
        _run([*arguments, str(temporary)], "生成高光集锦")
        actual = probe(temporary)
        if not actual["has_audio"] or not actual["has_video"]:
            raise ValueError("集锦结果缺少画面或音频。")
        if abs(actual["duration_ms"] - expected_ms) > 100:
            raise ValueError("集锦时长与输入片段总长相差超过 100 毫秒，未发布结果。")
        decode_check(temporary)
        result = {"duration_ms": actual["duration_ms"], "width": actual["width"],
                  "height": actual["height"], "sha256": sha256(temporary),
                  "bytes": temporary.stat().st_size}
        _publish(temporary, target)
    return result
