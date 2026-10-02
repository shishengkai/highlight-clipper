"""切片交付、离线预览及持久反馈。"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import uuid
from pathlib import Path
from urllib.parse import quote

from . import __version__, media
from .core import UserError, atomic_text, fingerprint, now, object_hash, project_lock, read_json, write_json
from .transcript import clip_srt

LABELS = ("必选", "可选", "不选", "选点对但范围不对")
ENCODING = {"version": 1, "video": "libx264", "audio": "aac"}


def stamp(ms: int) -> str:
    seconds, remainder = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{remainder:03}"


def selection_key(selection: dict, candidate: dict) -> dict:
    return {
        "source_sha256": selection["source_sha256"],
        "transcript_sha256": selection["transcript_sha256"],
        "start_ms": candidate["start_ms"], "end_ms": candidate["end_ms"],
        "encoding": ENCODING,
    }


def check_bundle(bundle: Path, identity: dict) -> dict:
    info = read_json(bundle / "info.json")
    if info.get("identity") != identity:
        raise UserError(f"{bundle.name}已有不同来源或切点的结果；请用新的 --output 保留版本")
    for name, field in (("clip.mp4", "sha256"), ("clip.auto.srt", "subtitle_sha256")):
        file = bundle / name
        if not file.is_file() or fingerprint(file) != info.get(field):
            raise UserError(f"{bundle.name}的交付文件缺失或被修改，请检查后使用新输出目录")
    return info


def validate_output(output: Path, selection: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    source_file = output / "source.json"
    identity = {"source_sha256": selection["source_sha256"], "transcript_sha256": selection["transcript_sha256"]}
    if source_file.exists():
        if read_json(source_file) != identity:
            raise UserError("输出目录已有其他视频或转写版本；请指定新的 --output")
    else:
        nonlocks = [p for p in output.iterdir() if p.name != ".highlight-clipper.lock"]
        if nonlocks:
            raise UserError("输出目录包含非本程序管理的内容，请选择空目录")
        write_json(source_file, identity)


def render(source: Path, transcript: dict, selection: dict, output: Path,
           ids: list[str] | None = None, progress=None) -> list[dict]:
    validate_output(output, selection)
    selected = selection["candidates"]
    if ids is not None:
        if len(ids) != len(set(ids)) or set(ids) - {c["id"] for c in selected}:
            raise UserError("--ids 有重复或不存在的候选")
        selected = [c for c in selected if c["id"] in ids]
    # 在耗时编码前统一检查字幕能否忠实裁切。
    subtitles = {c["id"]: clip_srt(transcript, c["start_ms"], c["end_ms"]) for c in selected}
    results = []
    for candidate in selected:
        cid = candidate["id"]
        bundle = output / cid
        identity = selection_key(selection, candidate)
        if bundle.exists():
            info = check_bundle(bundle, identity)
            results.append({"id": cid, "status": "cached", **info})
            if progress:
                progress({"status": "cached", "id": cid})
            continue
        if progress:
            progress({"status": "encoding", "id": cid, "duration_ms": candidate["duration_ms"]})
        stage = output / f".render-{cid}"
        stage.mkdir(exist_ok=True)
        intent = stage / "intent.json"
        if intent.exists() and read_json(intent) != identity:
            raise UserError(f"{cid}有尚未完成的不同切点，请使用新输出目录")
        write_json(intent, identity)
        destination = stage / "clip.mp4"
        if destination.exists():
            # media.cut 仅在完整验证后发布该文件；中断恢复仍再次核对解码与时长。
            info = media.probe(destination)
            if not info["has_video"] or not info["has_audio"] or abs(info["duration_ms"] - candidate["duration_ms"]) > 150:
                raise UserError(f"{cid}暂存视频不完整，请检查后使用新输出目录")
            media.decode_check(destination)
            info.update(sha256=fingerprint(destination), bytes=destination.stat().st_size)
        else:
            info = media.cut(source, destination, candidate["start_ms"], candidate["end_ms"])
        atomic_text(stage / "clip.auto.srt", subtitles[cid])
        info.update(identity=identity, subtitle_sha256=fingerprint(stage / "clip.auto.srt"),
                    decode_verified=True, verified_at=now())
        write_json(stage / "info.json", info)
        stage.rename(bundle)
        results.append({"id": cid, "status": "rendered", **info})
    return results


def candidate_key(selection: dict, candidate: dict) -> str:
    return object_hash({**selection_key(selection, candidate), "id": candidate["id"],
                        "core_start_ms": candidate["core_start_ms"], "core_end_ms": candidate["core_end_ms"],
                        "source_layer": candidate["source_layer"]})


def feedback(project: Path, candidate_id: str, label: str, note: str, key: str) -> dict:
    if label not in LABELS:
        raise UserError("未知反馈标签")
    path = project / "feedback.json"
    data = read_json(path) if path.exists() else {"schema_version": 1, "events": []}
    item = {"id": candidate_id, "candidate_key": key, "event_id": uuid.uuid4().hex,
            "label": label, "note": note, "updated_at": now()}
    data["events"].append(item)
    write_json(path, data)
    return item


def feedback_rows(project: Path, output: Path, selection: dict) -> tuple[list[dict], dict, list[dict]]:
    existing = {}
    path = output / "feedback.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("label") and row["label"] not in LABELS:
                    raise UserError("feedback.csv 中有未知标签；已保留原文件，请修正后再生成报告")
                if row.get("id"):
                    key = row.get("candidate_key") or "unbound-" + object_hash(row)
                    if key in existing:
                        raise UserError("feedback.csv 有重复的候选版本，已保留原文件")
                    existing[key] = row
    baseline_path = output / "feedback-sync.json"
    baseline = read_json(baseline_path).get("rows", {}) if baseline_path.exists() else {}
    latest = {}
    ledger = project / "feedback.json"
    if ledger.exists():
        for event in read_json(ledger).get("events", []):
            if event.get("candidate_key"):
                latest[event["candidate_key"]] = event
    rows, sync, conflicts = [], {}, []
    for candidate in selection["candidates"]:
        key = candidate_key(selection, candidate)
        prior = existing.pop(key, {})
        previous = baseline.get(key, {})
        event = latest.get(key, {})
        chosen = {"label": prior.get("label", ""), "note": prior.get("note", "")}
        manually_changed = bool(prior) and chosen != {"label": previous.get("label", ""), "note": previous.get("note", "")}
        new_event = bool(event) and event.get("event_id") != previous.get("ledger_event_id")
        if new_event:
            incoming = {"label": event["label"], "note": event["note"]}
            if manually_changed and chosen != incoming:
                # 保留用户刚填写的 CSV；把同时发生的命令反馈留下供复核。
                conflicts.append({"id": candidate["id"], "candidate_key": key, "csv": chosen,
                                  "command": incoming, "event_id": event["event_id"], "recorded_at": now()})
            elif not manually_changed:
                chosen = incoming
        rows.append({"id": candidate["id"], "title": candidate["title"], **chosen, "candidate_key": key})
        sync[key] = {**chosen, "ledger_event_id": event.get("event_id")}
    # 当前计划没引用的旧反馈也保留，避免修订计划丢掉历史。
    rows.extend({field: row.get(field, "") for field in ("id", "title", "label", "note", "candidate_key")} for row in existing.values())
    return rows, {"rows": {**baseline, **sync}}, conflicts


def montage(selection: dict, ids: list[str], output: Path, destination: Path, allow_overlap: bool) -> dict:
    by_id = {c["id"]: c for c in selection["candidates"]}
    if not ids or len(set(ids)) != len(ids) or set(ids) - set(by_id):
        raise UserError("集锦需明确列出不重复且存在的候选编号")
    selected = [by_id[cid] for cid in ids]
    if not allow_overlap and any(o["id"] in ids for c in selected for o in c["overlaps"]):
        raise UserError("集锦包含实际重叠的片段；请调整清单，或明确使用 --allow-overlap")
    infos = [check_bundle(output / c["id"], selection_key(selection, c)) for c in selected]
    identity = {"source_sha256": selection["source_sha256"], "transcript_sha256": selection["transcript_sha256"],
                "inputs": [{"id": cid, "sha256": info["sha256"]} for cid, info in zip(ids, infos)], "encoding": ENCODING}
    manifest_path = destination.with_suffix(".manifest.json")
    stage = destination.parent / f".{destination.name}.highlight-clipper"
    with project_lock(stage):
        if manifest_path.exists():
            previous = read_json(manifest_path)
            if previous.get("identity") != identity or not destination.is_file() or fingerprint(destination) != previous.get("verification", {}).get("sha256"):
                raise UserError("同名集锦或来源清单已存在且不匹配，请使用新的输出文件名")
            return {"status": "cached", "file": str(destination), "segments": previous["segments"]}
        intent = stage / "intent.json"
        if intent.exists():
            if read_json(intent) != identity:
                raise UserError("该文件名有不同集锦的未完成任务，请使用新的输出文件名")
        elif destination.exists() or destination.is_symlink():
            raise UserError("集锦目标文件已存在且未登记，拒绝覆盖")
        else:
            write_json(intent, identity)
        staged_video = stage / "montage.mp4"
        staged_manifest = stage / "manifest.json"
        if staged_manifest.exists():
            manifest = read_json(staged_manifest)
            retained = staged_video if staged_video.is_file() else destination
            if manifest.get("identity") != identity or not retained.is_file() or fingerprint(retained) != manifest.get("verification", {}).get("sha256"):
                raise UserError("集锦暂存结果不一致，请检查后另选输出文件名")
        else:
            if staged_video.exists():
                info = media.probe(staged_video)
                expected_duration = sum(i["duration_ms"] for i in infos)
                if abs(info["duration_ms"] - expected_duration) > max(150, 50 * len(infos)):
                    raise UserError("集锦暂存时长不匹配")
                media.decode_check(staged_video)
                info.update(sha256=fingerprint(staged_video), bytes=staged_video.stat().st_size)
            else:
                info = media.concat([output / cid / "clip.mp4" for cid in ids], staged_video)
            offset, mappings = 0, []
            for c, clip_info in zip(selected, infos):
                actual = clip_info["duration_ms"]
                mappings.append({"id": c["id"], "montage_start_ms": offset, "montage_end_ms": offset + actual,
                                 "source_start_ms": c["start_ms"], "source_end_ms": c["end_ms"],
                                 "source_layer": c["source_layer"]})
                offset += actual
            manifest = {"schema_version": 1, "identity": identity, "segments": mappings, "verification": info,
                        "note": "按明确清单拼接，可能包含不连续内容或明确允许的重叠；仍需观看验收。"}
            write_json(staged_manifest, manifest)
        if not destination.exists():
            # 同目录硬链接发布具有不覆盖语义；即使随后中断，暂存清单仍可验证恢复。
            os.link(staged_video, destination)
        elif fingerprint(destination) != manifest["verification"]["sha256"]:
            raise UserError("集锦目标已被其他内容占用，拒绝覆盖")
        os.link(staged_manifest, manifest_path)
        return {"status": "montage_ready", "file": str(destination), "segments": manifest["segments"]}


def report(project: Path, metadata: dict, selection: dict, output: Path) -> dict:
    validate_output(output, selection)
    rows, sync, conflicts = feedback_rows(project, output, selection)
    by_feedback = {r["candidate_key"]: r for r in rows}
    clips = []
    for candidate in selection["candidates"]:
        bundle = output / candidate["id"]
        info = check_bundle(bundle, selection_key(selection, candidate)) if bundle.exists() else None
        clips.append({**candidate, "rendered": info is not None,
                      "video": f"{candidate['id']}/clip.mp4" if info else None,
                      "subtitles": f"{candidate['id']}/clip.auto.srt" if info else None,
                      "verification": info, "feedback": by_feedback[candidate_key(selection, candidate)]})
    delivery = {
        "schema_version": 1, "tool_version": __version__, "generated_at": now(),
        "source": metadata["source"], "selection_sha256": object_hash(selection),
        "transcript_sha256": selection["transcript_sha256"],
        "source_structure": selection["source_structure"], "overview": selection["overview"],
        "first_watch": selection["first_watch"], "clips": clips,
        "checks": selection["checks"], "limitations": selection["limitations"],
        "quality_status": "file_checks_passed" if clips and all(c["rendered"] for c in clips) else "partial_or_not_rendered",
    }
    write_json(output / "selection.json", selection)
    write_json(output / "delivery.json", delivery)
    write_json(output / "magicdub-handoff.json", {
        "schema_version": 1, "base_directory": ".", "source_sha256": selection["source_sha256"],
        "note": "本文件列出待译制原声片段；不是 magicdub-cli 的原生批量配置。由宿主按该工具当前用法逐个交接。未自动翻译或发布。",
        "clips": [{"id": c["id"], "video": c["video"], "subtitles": c["subtitles"],
                   "title": c["title"], "source_layer": c["source_layer"],
                   "source_start_ms": c["start_ms"], "source_end_ms": c["end_ms"]} for c in clips if c["rendered"]],
    })
    csv_stream = io.StringIO(newline="")
    writer = csv.DictWriter(csv_stream, fieldnames=("id", "title", "label", "note", "candidate_key"))
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(output / "feedback.csv", csv_stream.getvalue())
    write_json(output / "feedback-sync.json", sync)
    if conflicts:
        conflict_path = output / "feedback-conflicts.json"
        previous = read_json(conflict_path) if conflict_path.exists() else {"events": []}
        previous["events"].extend(conflicts)
        write_json(conflict_path, previous)
    def esc(value):
        return html.escape(str(value), quote=True)
    overview = esc(selection["overview"])
    title = esc(metadata["source"]["title"])
    quick = " · ".join(f'<a href="#{esc(cid)}">{esc(cid)}</a>' for cid in selection["first_watch"])
    cards = []
    markdown = [f"# {metadata['source']['title']} — 高光候选", "", selection["overview"], "",
                f"视频结构：{selection['source_structure']}", "",
                "优先观看：" + "、".join(selection["first_watch"]), "", selection["limitations"], ""]
    for c in clips:
        time_range = f"{stamp(c['start_ms'])} → {stamp(c['end_ms'])}（{c['duration_ms']/1000:.2f} 秒）"
        overlaps = "、".join(f"{o['id']} ({o['duration_ms']/1000:.2f}s)" for o in c["overlaps"]) or "无"
        variant = c.get("variant_of") or "主候选"
        if c["rendered"]:
            video = f'<video controls preload="none" src="{quote(c["video"])}"></video><p><a href="{quote(c["subtitles"])}">自动原文 SRT</a></p>'
        else:
            video = "<p>尚未渲染；保留在完整候选库中。</p>"
        cards.append(f'''<article id="{esc(c['id'])}" data-search="{esc(c['title'] + ' ' + c['priority'] + ' ' + c['source_layer'])}">
<h2>{esc(c['id'])} · {esc(c['title'])}</h2><p class="meta">{esc(c['priority'])} · {esc(c['source_layer'])} · {esc(time_range)}</p>
{video}<p><strong>观看价值：</strong>{esc(c['reason'])}</p><p><strong>上下文：</strong>{esc(c['context_note'])}</p>
<p>变体归属：{esc(variant)}；实际时间重叠：{esc(overlaps)}</p>
<details><summary>定位与复核依据</summary><p>核心：{stamp(c['core_start_ms'])} → {stamp(c['core_end_ms'])}</p>
<p>原文句号：{esc(c['sentence_ids'])}；关联问题：{esc(c.get('question_ids', []))}；问题完整包含：{esc(c['question_fully_included'])}</p>
<pre>{esc(json.dumps(c['review'], ensure_ascii=False, indent=2))}</pre></details>
<p>反馈：{esc(c['feedback'].get('label') or '待评价')} {esc(c['feedback'].get('note', ''))}</p></article>''')
        markdown.extend([f"## {c['id']} · {c['title']}", "", f"{c['priority']} / {c['source_layer']} / {time_range}", "",
                         c["reason"], "", "上下文：" + c["context_note"], "",
                         f"变体归属：{variant}；实际时间重叠：{overlaps}", ""])
        if c["rendered"]:
            markdown.append(f"[原声视频]({quote(c['video'])}) · [自动原文字幕]({quote(c['subtitles'])})\n")
        else:
            markdown.append("尚未渲染。\n")
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · 高光候选</title><style>
body{{max-width:960px;margin:36px auto;padding:0 20px;font:16px/1.7 system-ui,sans-serif;background:#f5f5f3;color:#222}}
article{{background:white;border:1px solid #ddd;border-radius:12px;padding:22px;margin:24px 0}}h1{{line-height:1.3}}h2{{font-size:21px}}
video{{width:100%;max-height:540px;background:#111}}.meta,footer{{color:#555}}input{{box-sizing:border-box;width:100%;padding:12px;font:inherit}}pre{{white-space:pre-wrap}}a{{color:#165f98}}
</style><h1>{title}</h1><p>{overview}</p><p>结构：{esc(selection['source_structure'])}</p>
<p>优先观看：{quick or '无'}</p><p>全部候选 {len(clips)} 条，已渲染 {sum(c['rendered'] for c in clips)} 条；变体可能重复。</p>
<p>{esc(selection['limitations'])}</p><input id="filter" aria-label="筛选候选" placeholder="按标题、优先/备选、来源筛选">
{''.join(cards)}<footer>在 feedback.csv 填写反馈，或用 feedback 命令保存；本页不联网、不自动写入反馈。</footer>
<script>document.getElementById('filter').addEventListener('input',function(){{const q=this.value.toLowerCase();document.querySelectorAll('article').forEach(a=>a.hidden=!a.dataset.search.toLowerCase().includes(q));}});</script></html>'''
    atomic_text(output / "index.html", page)
    atomic_text(output / "README.md", "\n".join(markdown))
    return {"status": "reported", "candidates": len(clips), "rendered": sum(c["rendered"] for c in clips),
            "new_feedback_conflicts": len(conflicts),
            "preview": str(output / "index.html"), "delivery": str(output / "delivery.json")}
