"""项目文件、来源身份和写入锁。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class UserError(ValueError):
    """可直接向用户展示、无需堆栈的操作错误。"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UserError(f"无法读取有效 JSON：{path.name}") from exc
    if not isinstance(value, dict):
        raise UserError(f"JSON 顶层必须是对象：{path.name}")
    return value


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def safe_id(value: object, label: str = "编号") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise UserError(f"{label}只能含英文字母、数字、下划线或连字符，长度 1—80")
    return value


def source_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UserError("来源地址必须是无用户名密码的 HTTP(S) URL")
    # YouTube 的 v 是素材身份；其他查询参数可能包含签名，不写入项目或报告。
    if parsed.hostname.lower() in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        from urllib.parse import parse_qs, urlencode

        video = parse_qs(parsed.query).get("v", [])
        query = urlencode({"v": video[0]}) if video else ""
    else:
        query = ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def redact(message: str) -> str:
    for name, value in os.environ.items():
        if value and len(value) >= 4 and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET")):
            message = message.replace(value, "[已隐藏]")
    return re.sub(r"https?://\S+", "[网址已隐藏]", message)[:800]


@contextmanager
def project_lock(project: Path):
    """排除并发写入；仅自动恢复同机、已明确退出的 POSIX 进程锁。"""
    project.mkdir(parents=True, exist_ok=True)
    path = project / ".highlight-clipper.lock"
    token = uuid.uuid4().hex
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            previous = read_json(path)
            stale = False
            pid = previous.get("pid")
            if os.name == "posix" and previous.get("host") == socket.gethostname() and type(pid) is int and pid > 0:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    stale = True
                except PermissionError:
                    pass
            if stale and attempt == 0:
                # 先确认锁仍是刚才读取的那个，避免移除其他并发任务的新锁。
                if read_json(path) == previous:
                    path.unlink()
                    continue
            raise UserError("项目正被另一进程使用，或锁状态待核实；请先检查该进程") from None
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "host": socket.gethostname(), "token": token, "created_at": now()}, stream)
            break
    try:
        yield
    finally:
        if path.exists() and read_json(path).get("token") == token:
            path.unlink()


def load_project(project: Path, verify_source: bool = True) -> dict:
    metadata = read_json(project / "project.json")
    if metadata.get("schema_version") != 1 or not isinstance(metadata.get("source"), dict):
        raise UserError("不支持的项目格式")
    source = metadata["source"]
    if verify_source:
        path = Path(source["path"])
        if not path.is_file() or fingerprint(path) != source["sha256"]:
            raise UserError("源视频缺失或哈希已改变；请恢复原文件，或为新视频建立新项目")
    return metadata


def load_transcript(project: Path, metadata: dict) -> dict:
    from .transcript import validate

    data = read_json(project / "transcript.json")
    validate(data, metadata["source"]["sha256"], metadata["source"]["duration_ms"])
    return data
