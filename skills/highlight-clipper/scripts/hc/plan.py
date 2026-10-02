"""把宿主填写的编辑计划转换为来源可追溯的切片范围。"""

from __future__ import annotations

from .core import UserError, safe_id

PASSES = ("stories_examples", "ideas_conflicts", "language_interaction", "omission_review")


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UserError(f"{label}必须填写非空文字")
    return value.strip()


def integer(value: object, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise UserError(f"{label}必须是 {low}—{high} 范围内的整数")
    return value


def template(metadata: dict, transcript_sha256: str) -> dict:
    return {
        "schema_version": 1,
        "source_sha256": metadata["source"]["sha256"],
        "transcript_sha256": transcript_sha256,
        "source_structure": "",
        "overview": "",
        "passes": dict.fromkeys(PASSES, ""),
        "topics": [],
        "candidates": [],
        "first_watch": [],
    }


def resolve_range(value: object, sentences: list[dict], label: str) -> tuple[int, int, list[int]]:
    if not isinstance(value, dict):
        raise UserError(f"{label}必须包含 start_id/end_id")
    allowed = {"start_id", "end_id", "start_word", "end_word"}
    if set(value) - allowed:
        raise UserError(f"{label}包含未知字段；时间请通过句/词编号定位，人工修正使用 boundary_override")
    first = integer(value.get("start_id"), f"{label}.start_id", 1, len(sentences))
    last = integer(value.get("end_id"), f"{label}.end_id", first, len(sentences))
    left, right = sentences[first - 1], sentences[last - 1]
    # 重叠字幕的结束时间未必单调，完整句范围必须包含所有相交句尾。
    start = left["start_ms"]
    end = max(s["end_ms"] for s in sentences[first - 1:last])
    if "start_word" in value:
        words = left.get("words") or []
        if not words:
            raise UserError(f"{label}的起始句没有词级时间")
        i = integer(value["start_word"], f"{label}.start_word", 1, len(words))
        start = words[i - 1]["start_ms"]
    if "end_word" in value:
        words = right.get("words") or []
        if not words:
            raise UserError(f"{label}的末句没有词级时间")
        i = integer(value["end_word"], f"{label}.end_word", 1, len(words))
        end = words[i - 1]["end_ms"]
    if start >= end:
        raise UserError(f"{label}起止顺序错误")
    return start, end, list(range(first, last + 1))


def compile_plan(data: dict, metadata: dict, transcript: dict, transcript_sha256: str) -> dict:
    """只验证来源、结构和时间，不把主观编辑结论标为机器已验证。"""
    source = metadata["source"]
    if data.get("schema_version") != 1:
        raise UserError("不支持的编辑计划版本")
    if data.get("source_sha256") != source["sha256"]:
        raise UserError("编辑计划属于不同的源视频")
    if data.get("transcript_sha256") != transcript_sha256:
        raise UserError("转写已经变化，须复查并更新编辑计划；不可沿用旧句号和切点")
    structure = text(data.get("source_structure"), "视频结构")
    overview = text(data.get("overview"), "内容导览")
    passes = data.get("passes")
    if not isinstance(passes, dict):
        raise UserError("缺少分轮检查说明 passes")
    for name in PASSES:
        text(passes.get(name), f"检查说明 {name}")
    sentences = transcript["sentences"]
    duration = source["duration_ms"]
    candidates = data.get("candidates")
    topics = data.get("topics")
    if not isinstance(candidates, list) or not isinstance(topics, list) or not topics:
        raise UserError("candidates 必须是列表，topics 必须是覆盖全文的非空列表")
    result = []
    ids = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise UserError("每个候选必须是对象")
        cid = safe_id(candidate.get("id"), "候选编号")
        if cid in ids:
            raise UserError(f"候选编号重复：{cid}")
        ids.add(cid)
        for field in ("title", "reason", "context_note", "source_layer"):
            text(candidate.get(field), f"{cid}.{field}")
        if candidate.get("priority") not in ("优先", "备选"):
            raise UserError(f"{cid}.priority 应为优先或备选")
        types = candidate.get("types")
        if not isinstance(types, list) or not types or not all(isinstance(t, str) and t.strip() for t in types):
            raise UserError(f"{cid}.types 至少包含一种观看价值")
        review = candidate.get("review")
        if not isinstance(review, dict):
            raise UserError(f"{cid}需要说明归属、上下文和边界复核依据及限度")
        for field in ("attribution", "context", "boundary"):
            text(review.get(field), f"{cid}.review.{field}")
        start, end, sentence_ids = resolve_range(candidate.get("range"), sentences, f"{cid}.range")
        core_start, core_end, core_ids = resolve_range(candidate.get("core"), sentences, f"{cid}.core")
        if candidate.get("boundary_override") is not None:
            override = candidate["boundary_override"]
            if not isinstance(override, dict):
                raise UserError(f"{cid}.boundary_override 必须是对象")
            text(override.get("reason"), f"{cid}人工边界修正理由")
            text(override.get("evidence"), f"{cid}人工边界修正依据")
            start = integer(override.get("start_ms"), f"{cid}.start_ms", 0, duration - 1)
            end = integer(override.get("end_ms"), f"{cid}.end_ms", start + 1, duration)
        if not (0 <= start <= core_start < core_end <= end <= duration):
            raise UserError(f"{cid}的切片必须完整包含核心范围，且不超出源视频")
        question_ids = candidate.get("question_ids", [])
        if not isinstance(question_ids, list) or any(type(i) is not int or not 1 <= i <= len(sentences) for i in question_ids):
            raise UserError(f"{cid}.question_ids 包含无效句号")
        if len(set(question_ids)) != len(question_ids):
            raise UserError(f"{cid}.question_ids 不得重复")
        variant_of = candidate.get("variant_of")
        if variant_of is not None:
            safe_id(variant_of, f"{cid}.variant_of")
            if variant_of == cid:
                raise UserError("变体不能关联自己")
        declared_ids = sentence_ids
        sentence_ids = [s["id"] for s in sentences if s["start_ms"] < end and s["end_ms"] > start]
        result.append({
            **candidate, "start_ms": start, "end_ms": end, "duration_ms": end - start,
            "sentence_ids": sentence_ids, "declared_sentence_ids": declared_ids,
            "core_start_ms": core_start, "core_end_ms": core_end, "core_sentence_ids": core_ids,
            "question_fully_included": all(start <= sentences[i - 1]["start_ms"] and
                                            sentences[i - 1]["end_ms"] <= end for i in question_ids),
            "overlaps": [],
        })
    by_id = {item["id"]: item for item in result}
    for item in result:
        parent = item.get("variant_of")
        if parent:
            if parent not in by_id or by_id[parent].get("variant_of"):
                raise UserError(f"{item['id']}必须关联已有的主候选，不得形成变体链或循环")
            p = by_id[parent]
            if min(item["end_ms"], p["end_ms"]) <= max(item["start_ms"], p["start_ms"]):
                raise UserError(f"{item['id']}与主候选无时间重叠，请按独立候选记录")
        for other in result:
            overlap = min(item["end_ms"], other["end_ms"]) - max(item["start_ms"], other["start_ms"])
            if other["id"] != item["id"] and overlap > 0:
                item["overlaps"].append({"id": other["id"], "duration_ms": overlap})
    assigned = []
    topic_ids = set()
    mentioned = set()
    for topic in topics:
        if not isinstance(topic, dict):
            raise UserError("话题必须是对象")
        tid = safe_id(topic.get("id"), "话题编号")
        if tid in topic_ids:
            raise UserError(f"话题编号重复：{tid}")
        topic_ids.add(tid)
        for field in ("title", "source_layer", "audit_note"):
            text(topic.get(field), f"{tid}.{field}")
        first = integer(topic.get("start_id"), f"{tid}.start_id", 1, len(sentences))
        last = integer(topic.get("end_id"), f"{tid}.end_id", first, len(sentences))
        assigned.extend(range(first, last + 1))
        linked = topic.get("candidate_ids")
        if not isinstance(linked, list) or any(not isinstance(i, str) or i not in ids for i in linked):
            raise UserError(f"{tid}.candidate_ids 引用了不存在的候选")
        if len(linked) != len(set(linked)):
            raise UserError(f"{tid}.candidate_ids 不得重复")
        for cid in linked:
            if not set(range(first, last + 1)).intersection(by_id[cid]["sentence_ids"]):
                raise UserError(f"{tid}与所关联候选 {cid} 没有共同原文句子")
        mentioned.update(linked)
    if assigned != list(range(1, len(sentences) + 1)):
        raise UserError("话题表必须按原文顺序完整覆盖全部句号，不能遗漏、重复或倒序")
    if mentioned != ids:
        raise UserError("所有候选（包括长度变体）都必须关联到话题表")
    first_watch = data.get("first_watch")
    if not isinstance(first_watch, list) or any(not isinstance(i, str) or i not in ids for i in first_watch):
        raise UserError("first_watch 必须只引用已有候选")
    if len(first_watch) != len(set(first_watch)) or (candidates and not first_watch):
        raise UserError("有候选时应提供不重复的优先观看入口")
    return {
        "schema_version": 1, "source_sha256": source["sha256"],
        "transcript_sha256": transcript_sha256, "source_duration_ms": duration,
        "source_structure": structure, "overview": overview, "passes": passes,
        "topics": topics, "candidates": result, "first_watch": first_watch,
        "checks": {"all_sentences_assigned_once": True, "ranges_valid": True},
        "limitations": "程序只验证来源身份、字段、覆盖结构与时间范围；不证明高光零漏选、原话准确、完整听审或传播效果。",
    }
