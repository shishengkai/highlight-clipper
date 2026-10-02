"""中文命令行入口；编辑判断由读取 SKILL.md 的宿主完成。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, delivery, media, plan, transcript
from .core import (
    UserError, fingerprint, load_project, load_transcript, now, project_lock,
    read_json, redact, safe_id, source_url, write_json,
)


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def absolute(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def save_transcript(project: Path, metadata: dict, input_path: Path, replace: bool = False) -> dict:
    normalized = transcript.normalize(input_path, metadata["source"]["sha256"],
                                      metadata["source"]["duration_ms"], metadata["language"])
    destination = project / "transcript.json"
    if destination.exists():
        old = read_json(destination)
        if old == normalized:
            # JSON 已发布而派生文件尚未写完时，重跑会补全可阅读稿与 SRT。
            transcript.write_reading(normalized, project / "transcript-reading.txt")
            transcript.write_srt(normalized, project / "source.auto.srt")
            return {"status": "cached", "sentences": len(normalized["sentences"]),
                    "precision": normalized["precision"], "transcript_sha256": fingerprint(destination),
                    "reading": str(project / "transcript-reading.txt")}
        if not replace:
            raise UserError("项目已有不同转写；复查后使用 --replace 保留旧稿并更新，新稿会使旧选片计划失效")
        write_json(project / "history" / f"transcript-{fingerprint(destination)}.json", old)
    write_json(destination, normalized)
    transcript.write_reading(normalized, project / "transcript-reading.txt")
    transcript.write_srt(normalized, project / "source.auto.srt")
    return {"status": "imported", "sentences": len(normalized["sentences"]),
            "precision": normalized["precision"], "transcript_sha256": fingerprint(destination),
            "reading": str(project / "transcript-reading.txt")}


def prepare(args) -> dict:
    video = absolute(args.video)
    project = absolute(args.project)
    if not video.is_file():
        raise UserError("找不到本地源视频")
    if (project / ".git").exists():
        raise UserError("请用独立的素材项目子目录，不要把 Git 仓库根目录当作素材项目")
    with project_lock(project):
        probe = media.probe(video)
        if not probe["has_audio"] or not probe["has_video"]:
            raise UserError("源素材必须同时包含视频和原声音轨")
        source = {"path": str(video), "sha256": fingerprint(video),
                  "duration_ms": probe["duration_ms"], "width": probe["width"], "height": probe["height"],
                  "title": args.title or video.stem, "url": source_url(args.source_url)}
        if (project / "project.json").exists():
            metadata = load_project(project)
            if metadata["source"]["sha256"] != source["sha256"]:
                raise UserError("该项目已绑定另一份视频；请创建新目录")
            if metadata["language"] != args.language:
                raise UserError("该项目的语言不同；请沿用原语言或创建新目录")
        else:
            unexpected = [p for p in project.iterdir() if p.name != ".highlight-clipper.lock"]
            if unexpected:
                raise UserError("素材项目目录需为空；请将源视频放在目录外并选择新的 --project")
            metadata = {"schema_version": 1, "created_at": now(), "language": args.language, "source": source}
            write_json(project / "project.json", metadata)
        result = {"status": "prepared", "project": str(project), "source": metadata["source"]}
        if args.subtitles:
            result["transcript"] = save_transcript(project, metadata, absolute(args.subtitles))
        return result


def compiled(project: Path, plan_path: Path) -> tuple[dict, dict, dict]:
    metadata = load_project(project)
    data = load_transcript(project, metadata)
    selection = plan.compile_plan(read_json(plan_path), metadata, data, fingerprint(project / "transcript.json"))
    return metadata, data, selection


def output_path(args, project: Path) -> Path:
    output = absolute(args.output) if args.output else project / "clips"
    if output == project or output in project.parents or (output / ".git").exists():
        raise UserError("输出目录须为独立子目录或新的外部目录")
    return output


def run(args) -> dict:
    if args.command == "doctor":
        return {"status": "ready" if all(shutil.which(p) for p in ("ffmpeg", "ffprobe")) else "missing_dependencies",
                "python": ".".join(map(str, sys.version_info[:3])), "tool_version": __version__,
                "programs": {p: bool(shutil.which(p)) for p in ("ffmpeg", "ffprobe")},
                "environment_keys_present": {p: bool(os.environ.get(p)) for p in ("SNAPANY_API_KEY", "DASHSCOPE_API_KEY", "FAL_KEY")},
                "note": "本地视频加可靠字幕不需要 API Key；不自动读取配置文件或安装任何程序。"}
    if args.command == "fetch":
        from .providers import fetch

        destination = absolute(args.output)
        if (destination / ".git").exists():
            raise UserError("下载目录不能是 Git 仓库根目录")
        with project_lock(destination):
            return fetch(args.url, destination)
    if args.command == "prepare":
        return prepare(args)
    project = absolute(args.project)
    if not (project / "project.json").is_file():
        raise UserError("找不到项目，请先运行 prepare")
    if args.command == "status":
        metadata = load_project(project, verify_source=False)
        task = project / "asr" / "asr-job.json"
        job = read_json(task) if task.exists() else {}
        return {"project": str(project), "source": metadata["source"],
                "transcript_ready": (project / "transcript.json").exists(),
                "plan_ready": (project / "analysis.json").exists(),
                "asr": {k: job[k] for k in ("status", "task_id", "usage") if k in job},
                "note": "这里只列本地状态，未查询远端 ASR 或重验源视频哈希。"}
    with project_lock(project):
        if args.command == "import-transcript":
            return save_transcript(project, load_project(project), absolute(args.input), args.replace)
        if args.command == "transcribe":
            from .providers import transcribe

            metadata = load_project(project)
            existing = project / "transcript.json"
            # 有字幕而希望改用 ASR 时，先明确替换意图，避免完成付费任务后才发现冲突。
            if existing.exists() and not args.replace and not (project / "asr" / "asr-job.json").exists():
                raise UserError("项目已有字幕；如需词级 ASR，请加 --replace，旧稿会保留")
            source = metadata["source"]
            result = transcribe(Path(source["path"]), project / "asr", source["sha256"],
                                source["duration_ms"], metadata["language"], args.model,
                                args.audio_url, args.poll_seconds)
            raw = result.pop("raw_transcript", None)
            if raw is not None:
                raw_path = project / "asr" / "transcript-import.json"
                write_json(raw_path, raw)
                result["transcript"] = save_transcript(project, metadata, raw_path, args.replace)
            return result
        if args.command == "read":
            metadata = load_project(project)
            data = load_transcript(project, metadata)
            start = args.start_id if args.start_id is not None else 1
            end = args.end_id if args.end_id is not None else len(data["sentences"])
            plan.integer(start, "start-id", 1, len(data["sentences"]))
            plan.integer(end, "end-id", start, len(data["sentences"]))
            sentences = []
            for sentence in data["sentences"][start - 1:end]:
                item = {k: v for k, v in sentence.items() if k != "words"}
                if args.words:
                    item["words"] = [{"word_id": i, **w} for i, w in enumerate(sentence.get("words", []), 1)]
                sentences.append(item)
            return {"total_sentences": len(data["sentences"]), "returned_range": [start, end],
                    "precision": data["precision"], "sentences": sentences}
        if args.command == "frame":
            metadata = load_project(project)
            plan.integer(args.at_ms, "at-ms", 0, metadata["source"]["duration_ms"] - 1)
            dest = absolute(args.output)
            media.frame(Path(metadata["source"]["path"]), dest, args.at_ms)
            return {"status": "frame_ready", "at_ms": args.at_ms, "file": str(dest),
                    "note": "已导出画面；需由宿主实际查看才能记作视觉复核。"}
        if args.command == "audio":
            metadata = load_project(project)
            destination = absolute(args.output)
            media.extract_audio(Path(metadata["source"]["path"]), destination)
            return {"status": "audio_ready", "file": str(destination), "sha256": fingerprint(destination),
                    "source_sha256": metadata["source"]["sha256"]}
        if args.command == "plan-template":
            metadata = load_project(project)
            load_transcript(project, metadata)
            dest = absolute(args.output) if args.output else project / "analysis.json"
            if dest.exists():
                raise UserError("编辑计划已存在，保留原文件；如需新版本请指定新的 --output")
            write_json(dest, plan.template(metadata, fingerprint(project / "transcript.json")))
            return {"status": "template_ready", "file": str(dest), "note": "请由宿主阅读全文并填写编辑判断；空模板不会通过校验。"}
        if args.command == "feedback":
            chosen_plan = absolute(args.plan) if args.plan else project / "analysis.json"
            _, _, selection = compiled(project, chosen_plan)
            cid = safe_id(args.id)
            candidate = next((c for c in selection["candidates"] if c["id"] == cid), None)
            if candidate is None:
                raise UserError("反馈编号不在该编辑计划中")
            return delivery.feedback(project, cid, args.label, args.note or "", delivery.candidate_key(selection, candidate))
        metadata, data, selection = compiled(project, absolute(args.plan))
        if args.command == "validate":
            # 预检能否生成字幕，防止只凭合法数字错误地认可截断的句子。
            for candidate in selection["candidates"]:
                transcript.clip_srt(data, candidate["start_ms"], candidate["end_ms"])
            return {"status": "valid", "candidates": len(selection["candidates"]),
                    "topics": len(selection["topics"]), "checks": selection["checks"],
                    "limitations": selection["limitations"]}
        if args.command == "montage":
            output = absolute(args.output_dir) if args.output_dir else project / "clips"
            return delivery.montage(selection, args.ids, output, absolute(args.output), args.allow_overlap)
        output = output_path(args, project)
        with project_lock(output):
            if args.command == "render":
                delivery.render(Path(metadata["source"]["path"]), data, selection, output, args.ids, progress=emit)
            return delivery.report(project, metadata, selection, output)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="高光切片：Codex 做编辑判断，Python 处理媒体与可追溯交付。")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查 Python、FFmpeg 和环境变量名称，不联网、不安装")
    fetch = sub.add_parser("fetch", help="下载 YouTube 或 HTTPS 直链视频")
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--output", required=True)
    prep = sub.add_parser("prepare", help="绑定源视频并可选导入字幕")
    prep.add_argument("--video", required=True)
    prep.add_argument("--project", required=True)
    prep.add_argument("--subtitles")
    prep.add_argument("--language", default="en")
    prep.add_argument("--title")
    prep.add_argument("--source-url")
    imp = sub.add_parser("import-transcript", help="导入 SRT、VTT 或支持的转写 JSON")
    imp.add_argument("--project", required=True)
    imp.add_argument("--input", required=True)
    imp.add_argument("--replace", action="store_true", help="保留旧稿后替换；旧计划需重新复核")
    asr = sub.add_parser("transcribe", help="提交或恢复 ASR 任务；密钥仅从环境变量读取")
    asr.add_argument("--project", required=True)
    asr.add_argument("--audio-url", help="自行托管本工具从绑定视频提取的同一份 WAV；下载核验哈希后使用，免 FAL 上传")
    asr.add_argument("--model", help="可选 DashScope ASR 模型，默认由适配器设置")
    asr.add_argument("--poll-seconds", type=float, default=0, help="本次最多查询秒数（0—60），0 表示查询一次")
    asr.add_argument("--replace", action="store_true", help="保留已有字幕后换为 ASR 时间轴")
    read = sub.add_parser("read", help="读取原文及句/词编号，支持分段复读")
    read.add_argument("--project", required=True)
    read.add_argument("--start-id", type=int)
    read.add_argument("--end-id", type=int)
    read.add_argument("--words", action="store_true")
    frame = sub.add_parser("frame", help="抽取待查看的源视频画面")
    frame.add_argument("--project", required=True)
    frame.add_argument("--at-ms", type=int, required=True)
    frame.add_argument("--output", required=True)
    audio = sub.add_parser("audio", help="提取源视频的 16k 单声道 WAV，可自行托管给 ASR")
    audio.add_argument("--project", required=True)
    audio.add_argument("--output", required=True)
    tpl = sub.add_parser("plan-template", help="生成由 Codex 填写的编辑计划模板")
    tpl.add_argument("--project", required=True)
    tpl.add_argument("--output")
    for name, help_text in (("validate", "验证来源、全文覆盖结构及边界"),
                            ("render", "渲染候选与自动字幕并生成报告"), ("report", "更新离线预览和交付报告")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--project", required=True)
        cmd.add_argument("--plan", required=True)
        if name != "validate":
            cmd.add_argument("--output")
        if name == "render":
            cmd.add_argument("--ids", nargs="+", help="只渲染这些编号；报告仍保留全部候选")
    feedback = sub.add_parser("feedback", help="保存用户偏好，追加历史而不改写旧反馈")
    feedback.add_argument("--project", required=True)
    feedback.add_argument("--id", required=True)
    feedback.add_argument("--label", choices=delivery.LABELS, required=True)
    feedback.add_argument("--note")
    feedback.add_argument("--plan", help="反馈对应的编辑计划，默认项目 analysis.json")
    status = sub.add_parser("status", help="查看本地处理状态，不发起 API 查询")
    status.add_argument("--project", required=True)
    montage = sub.add_parser("montage", help="按明确顺序拼接已渲染片段并保存来源映射")
    montage.add_argument("--project", required=True)
    montage.add_argument("--plan", required=True)
    montage.add_argument("--ids", nargs="+", required=True)
    montage.add_argument("--output-dir", help="已有切片目录，默认项目 clips")
    montage.add_argument("--output", required=True)
    montage.add_argument("--allow-overlap", action="store_true", help="明确允许清单中的实际时间重叠")
    return p


def main(argv: list[str] | None = None) -> int:
    if sys.version_info[:2] != (3, 12):
        emit({"status": "error", "detail": "请使用 Python 3.12 运行本技能"})
        return 2
    args = parser().parse_args(argv)
    try:
        result = run(args)
        emit(result)
        failed = {"failed", "cancelled", "submit_rejected", "submission_unknown", "submitting", "missing_dependencies"}
        return 1 if result.get("status") in failed else 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        emit({"status": "error", "detail": redact(str(exc)), "error_type": type(exc).__name__})
        return 1
    except KeyboardInterrupt:
        emit({"status": "interrupted", "detail": "已中断；保存的任务和完成片段会保留，重跑前先查看 status"})
        return 130
