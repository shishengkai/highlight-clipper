"""规范字幕与 ASR 时间轴；保留原文、重叠和精度来源。"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path


_TIME = r"(?:\d{2,}:)?\d{2}:\d{2}[,.]\d{3}"
_TIMING = re.compile(rf"^({_TIME})\s+-->\s+({_TIME})(?:[ \t]+(.*))?$")
_HASH = re.compile(r"[0-9a-f]{64}")


def stamp(milliseconds: int, separator: str = ",") -> str:
    """以小时开头，避免超过一小时后时间轴回绕。"""
    seconds, remainder = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{remainder:03}"


def _number(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label}必须是整数毫秒。")
    return value


def _stamp_value(value: str) -> int:
    sections = value.replace(",", ".").split(":")
    if len(sections) == 2:
        sections.insert(0, "0")
    hours, minutes = int(sections[0]), int(sections[1])
    seconds, milliseconds = (int(item) for item in sections[2].split("."))
    if minutes >= 60 or seconds >= 60:
        raise ValueError("字幕的分或秒必须小于 60。")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}必须包含非空文字。")
    if "\x00" in value:
        raise ValueError(f"{label}包含无效控制字符。")
    return value.strip()


def _subtitle_text(lines: list[str]) -> tuple[str, str | None]:
    body = "\n".join(lines)
    voice = re.search(r"<v(?:\.[^ >]+)*\s+([^>]+)>", body)
    speaker = html.unescape(voice.group(1)).strip() if voice else None
    # 只去除标准字幕样式和内嵌时间标签；未知标签不被静默吞掉。
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(rf"<{_TIME}>", "", body)
    body = re.sub(r"</?(?:b|i|u|ruby|rt|c|v|lang|font)(?:[ .][^<>]*)?>", "", body,
                  flags=re.I)
    if re.search(r"</?[A-Za-z][^<>]*>", body):
        raise ValueError("字幕包含尚未支持的标签，请保留原文并先转换为普通字幕。")
    return _text(html.unescape(body), "字幕正文"), speaker


def _read_subtitles(content: str, is_vtt: bool) -> list[dict]:
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n[ \t]*\n", content)
    if is_vtt:
        if not blocks or not re.match(r"^WEBVTT(?:[ \t].*)?(?:\n|$)", blocks[0]):
            raise ValueError("VTT 文件缺少 WEBVTT 文件头。")
        header_lines = blocks.pop(0).splitlines()[1:]
        if any(not line.startswith(("Kind:", "Language:")) for line in header_lines):
            raise ValueError("VTT 文件头含有无法识别的行，或文件头后缺少空行。")
    sentences = []
    for block_index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if is_vtt and lines and (lines[0] in {"STYLE", "REGION"}
                                 or re.match(r"^NOTE(?:[ \t]|$)", lines[0])):
            # VTT 的注释、样式和区域块属于明确定义的非语音元数据。
            if any("-->" in line for line in lines[1:]):
                raise ValueError(f"VTT 第 {block_index} 块的元数据中含时间行，请检查分隔空行。")
            continue
        if not lines:
            raise ValueError(f"字幕第 {block_index} 块为空。")
        if is_vtt:
            time_index = 0 if "-->" in lines[0] else 1
        else:
            if not lines[0].isdigit():
                raise ValueError(f"SRT 第 {block_index} 块缺少数字序号。")
            time_index = 1
        if len(lines) <= time_index + 1:
            raise ValueError(f"字幕第 {block_index} 块缺少时间或正文。")
        timing = _TIMING.fullmatch(lines[time_index].strip())
        if timing is None:
            raise ValueError(f"字幕第 {block_index} 块的时间格式无法识别。")
        settings = timing.group(3)
        if settings:
            if not is_vtt or any(not re.fullmatch(r"(?:vertical|line|position|size|align|region):\S+", item)
                                 for item in settings.split()):
                raise ValueError(f"字幕第 {block_index} 块含有无法识别的时间行内容。")
        if any("-->" in line for line in lines[time_index + 1:]):
            raise ValueError(f"字幕第 {block_index} 块包含额外时间行，请检查块间空行。")
        text, speaker_id = _subtitle_text(lines[time_index + 1:])
        sentences.append({"id": len(sentences) + 1,
                          "start_ms": _stamp_value(timing.group(1)),
                          "end_ms": _stamp_value(timing.group(2)), "text": text,
                          "speaker_id": speaker_id, "words": []})
    return sentences


def _normalize_sentence(sentence: object, index: int) -> dict:
    if not isinstance(sentence, dict):
        raise ValueError(f"第 {index} 句不是有效对象。")
    if "id" in sentence and (type(sentence["id"]) is not int or sentence["id"] != index):
        raise ValueError("已有句子 ID 必须从 1 连续递增；不会重新排序或悄悄重编号。")
    text = _text(sentence.get("text"), f"第 {index} 句正文")
    words = sentence.get("words", [])
    if not isinstance(words, list):
        raise ValueError(f"第 {index} 句的 words 必须是列表。")
    normalized_words = []
    for position, word in enumerate(words, 1):
        if not isinstance(word, dict):
            raise ValueError(f"第 {index} 句第 {position} 个词不是有效对象。")
        word_text = word.get("text")
        if not isinstance(word_text, str) or not word_text.strip():
            raise ValueError(f"第 {index} 句第 {position} 个词缺少正文。")
        punctuation = word.get("punctuation", "")
        if not isinstance(punctuation, str):
            raise ValueError("词的标点必须是文字。")
        if punctuation and not word_text.rstrip().endswith(punctuation):
            word_text += punctuation
        normalized_words.append({
            "start_ms": _number(word.get("start_ms", word.get("begin_time")), "词起始时间"),
            "end_ms": _number(word.get("end_ms", word.get("end_time")), "词结束时间"),
            "text": word_text,
        })
    return {"id": index,
            "start_ms": _number(sentence.get("start_ms", sentence.get("begin_time")), "句起始时间"),
            "end_ms": _number(sentence.get("end_ms", sentence.get("end_time")), "句结束时间"),
            "text": text, "speaker_id": sentence.get("speaker_id"), "words": normalized_words}


def normalize(input_path: Path, source_sha256: str, duration_ms: int,
              language: str = "en") -> dict:
    """导入 SRT、VTT、实验转写 JSON 或单音轨 DashScope 结果。"""
    path = Path(input_path)
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ValueError("无法以 UTF-8 读取字幕或转写文件。") from None
    suffix = path.suffix.lower()
    if suffix in {".srt", ".vtt"}:
        sentences = _read_subtitles(content, suffix == ".vtt")
        provider = suffix[1:]
        precision = "subtitle"
    elif suffix == ".json":
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            raise ValueError("转写 JSON 格式错误。") from None
        if not isinstance(raw, dict):
            raise ValueError("转写 JSON 顶层必须是对象。")
        if "schema_version" in raw and (type(raw["schema_version"]) is not int or raw["schema_version"] != 1):
            raise ValueError("不支持此转写数据版本。")
        if "source_sha256" in raw and raw["source_sha256"] != source_sha256:
            raise ValueError("转写文件的原视频指纹不匹配，不能用于当前素材。")
        if "source_duration_ms" in raw and raw["source_duration_ms"] != duration_ms:
            raise ValueError("转写文件记录的原视频时长与当前素材不同。")
        if raw.get("timeline_origin_ms", 0) != 0:
            raise ValueError("转写文件使用了非零时间原点，请先显式校准。")
        if "transcripts" in raw:
            tracks = raw["transcripts"]
            if not isinstance(tracks, list) or len(tracks) != 1 or not isinstance(tracks[0], dict):
                raise ValueError("只支持单一音轨的 ASR 结果；不能把多个声道混合为同一时间轴。")
            source_sentences = tracks[0].get("sentences")
            provider = "dashscope"
        else:
            source_sentences = raw.get("sentences")
            provider = raw.get("provider", "imported-asr")
        if not isinstance(source_sentences, list):
            raise ValueError("转写 JSON 缺少 sentences 列表。")
        channels = {sentence.get("channel_id") for sentence in source_sentences
                    if isinstance(sentence, dict) and type(sentence.get("channel_id")) in (str, int)}
        if len(channels) > 1:
            raise ValueError("句子来自多个声道，不能混合为同一条转写时间轴。")
        sentences = [_normalize_sentence(sentence, index)
                     for index, sentence in enumerate(source_sentences, 1)]
        precision = "word" if sentences and all(s["words"] for s in sentences) else "sentence"
        if "precision" in raw:
            precision = raw["precision"]
        if "language" in raw:
            language = raw["language"]
    else:
        raise ValueError("仅支持 .srt、.vtt 和 .json 字幕或转写文件。")
    result = {"schema_version": 1, "source_sha256": source_sha256,
              "source_duration_ms": duration_ms, "language": language,
              "provider": provider, "precision": precision, "sentences": sentences}
    validate(result, source_sha256, duration_ms)
    return result


def validate(data: dict, source_sha256: str, duration_ms: int) -> None:
    """校验绑定的素材、精度声明和所有句/词时间；允许句子重叠。"""
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("转写数据必须使用 schema_version: 1。")
    if not isinstance(source_sha256, str) or not _HASH.fullmatch(source_sha256):
        raise ValueError("原视频指纹必须是小写的 SHA-256。")
    if data.get("source_sha256") != source_sha256:
        raise ValueError("转写数据的原视频指纹不匹配。")
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("原视频时长必须是正整数毫秒。")
    if type(data.get("source_duration_ms")) is not int or data["source_duration_ms"] != duration_ms:
        raise ValueError("转写数据的原视频时长不匹配。")
    for key in ("language", "provider"):
        _text(data.get(key), key)
    precision = data.get("precision")
    if precision not in {"subtitle", "sentence", "word"}:
        raise ValueError("转写精度只能是 subtitle、sentence 或 word。")
    sentences = data.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        raise ValueError("转写或字幕为空，不能用于高光筛选。")
    previous_start = -1
    for index, sentence in enumerate(sentences, 1):
        if not isinstance(sentence, dict):
            raise ValueError(f"第 {index} 句不是有效对象。")
        if type(sentence.get("id")) is not int or sentence["id"] != index:
            raise ValueError("句子 ID 必须从 1 连续递增。")
        start = _number(sentence.get("start_ms"), "句起始时间")
        end = _number(sentence.get("end_ms"), "句结束时间")
        if not 0 <= start < end <= duration_ms:
            raise ValueError(f"第 {index} 句时间为空、倒置或超出原视频。")
        if start < previous_start:
            raise ValueError("句子必须按起始时间排列，不能静默排序原始内容。")
        previous_start = start
        _text(sentence.get("text"), f"第 {index} 句正文")
        if sentence.get("speaker_id") is not None and type(sentence["speaker_id"]) not in (str, int):
            raise ValueError("speaker_id 只能是文字、整数或 null；它不代表已核实的人物身份。")
        words = sentence.get("words")
        if not isinstance(words, list):
            raise ValueError(f"第 {index} 句必须提供 words 列表，没有词时间时使用空列表。")
        if precision == "word" and not words:
            raise ValueError("声明为词级转写时，每句必须有有效词时间。")
        if precision == "subtitle" and words:
            raise ValueError("字幕显示时间不能声明成词时间。")
        previous_word_start = -1
        for word in words:
            if not isinstance(word, dict):
                raise ValueError(f"第 {index} 句的词不是有效对象。")
            word_start = _number(word.get("start_ms"), "词起始时间")
            word_end = _number(word.get("end_ms"), "词结束时间")
            if not start <= word_start < word_end <= end:
                raise ValueError(f"第 {index} 句存在越过句子边界、倒置或为空的词时间。")
            if word_start < previous_word_start:
                raise ValueError(f"第 {index} 句的词没有按时间排列。")
            previous_word_start = word_start
            _text(word.get("text"), "词正文")


def write_reading(data: dict, output: Path) -> None:
    """生成包含全部句子的阅读稿，不通过摘要代替原文。"""
    validate(data, data.get("source_sha256"), data.get("source_duration_ms"))
    lines = [f"时间精度：{data['precision']}；说话人标签未经人物身份核实。", ""]
    for sentence in data["sentences"]:
        speaker = sentence.get("speaker_id")
        label = "未知" if speaker is None else str(speaker)
        lines.append(f"[S{sentence['id']:04d} {stamp(sentence['start_ms'], '.')} → "
                     f"{stamp(sentence['end_ms'], '.')} | speaker={label}]\n{sentence['text']}\n")
    Path(output).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cjk(character: str) -> bool:
    return ("\u2e80" <= character <= "\u9fff" or "\uf900" <= character <= "\ufaff"
            or "\uac00" <= character <= "\ud7af" or "\U00020000" <= character <= "\U000323af")


def _join_words(words: list[dict]) -> str:
    result = ""
    for word in words:
        part = word["text"]
        if result and part and not result[-1].isspace() and not part[0].isspace():
            left, right = result[-1], part[0]
            if (left.isalnum() and right.isalnum() and not _cjk(left) and not _cjk(right)) or (
                    left in ",;:!?" and right.isalnum() and not _cjk(right)):
                result += " "
        result += part
    return result.strip()


def _fragment(sentence: dict, positions: list[int]) -> str:
    """优先沿用原句字符间隔，保留英文空格、中文连写和标点。"""
    if positions != list(range(positions[0], positions[-1] + 1)):
        # 重叠词时间可能导致选中项不连续，此时不能用原句的一整段替代。
        return _join_words([sentence["words"][position] for position in positions])
    spans = []
    cursor = 0
    for word in sentence["words"]:
        token = word["text"].strip()
        start = sentence["text"].find(token, cursor)
        if start < 0:
            return _join_words([sentence["words"][position] for position in positions])
        cursor = start + len(token)
        spans.append((start, cursor))
    first, last = positions[0], positions[-1]
    begin, end = spans[first][0], spans[last][1]
    next_begin = spans[last + 1][0] if last + 1 < len(spans) else len(sentence["text"])
    following = sentence["text"][end:next_begin]
    if not any(character.isalnum() for character in following):
        end = next_begin
    return sentence["text"][begin:end].strip()


def clip_srt(data: dict, start_ms: int, end_ms: int) -> str:
    """生成相对切片的 SRT；缺少词时间时不伪造半句字幕。"""
    validate(data, data.get("source_sha256"), data.get("source_duration_ms"))
    if (type(start_ms) is not int or type(end_ms) is not int
            or not 0 <= start_ms < end_ms <= data["source_duration_ms"]):
        raise ValueError("字幕切片范围必须是原视频内有效的整数毫秒区间。")
    cues = []
    for sentence in data["sentences"]:
        begin, finish = sentence["start_ms"], sentence["end_ms"]
        if finish <= start_ms or begin >= end_ms:
            continue
        if begin < start_ms or finish > end_ms:
            if not sentence["words"]:
                raise ValueError(f"切片边界穿过第 {sentence['id']} 句，但没有词级时间；请扩展到句边界或补充词级转写。")
            positions = [index for index, word in enumerate(sentence["words"])
                         if start_ms <= word["start_ms"] and word["end_ms"] <= end_ms]
            if not positions:
                continue
            begin = sentence["words"][positions[0]]["start_ms"]
            finish = max(sentence["words"][position]["end_ms"] for position in positions)
            text = _fragment(sentence, positions)
        else:
            text = sentence["text"]
        cues.append(f"{len(cues) + 1}\n{stamp(begin - start_ms)} --> {stamp(finish - start_ms)}\n{text}")
    return "\n\n".join(cues) + ("\n" if cues else "")


def write_srt(data: dict, output: Path) -> None:
    """导出完整原视频字幕；保留重叠区间。"""
    Path(output).write_text(clip_srt(data, 0, data["source_duration_ms"]), encoding="utf-8")
