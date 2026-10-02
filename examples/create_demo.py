#!/usr/bin/env python3.12
"""生成无需联网的合成演示。不会安装技能、访问 API 或使用真实人物素材。"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True, help="新建演示目录，已存在则拒绝覆盖")
    args = p.parse_args()
    if sys.version_info[:2] != (3, 12):
        p.error("请使用 Python 3.12")
    root = Path(__file__).resolve().parents[1]
    skill = root / "skills" / "highlight-clipper"
    cli = skill / "scripts" / "highlight_clipper.py"
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    video = output / "synthetic.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=25:duration=13", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=13", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)], check=True)
    srt = output / "synthetic.srt"
    lines = [
        ("00:00:00,000", "00:00:02,000", "Why did your first attempt fail?"),
        ("00:00:02,000", "00:00:04,000", "I tried to learn everything before building anything."),
        ("00:00:04,000", "00:00:06,500", "Then I made one tiny prototype and learned where I was wrong."),
        ("00:00:06,500", "00:00:08,000", "So what changed?"),
        ("00:00:08,000", "00:00:10,500", "A useful mistake taught me more than a perfect plan."),
        ("00:00:10,500", "00:00:13,000", "That is a personal lesson, not a rule for every situation."),
    ]
    srt.write_text("\n\n".join(f"{i}\n{a} --> {b}\n{t}" for i, (a, b, t) in enumerate(lines, 1)) + "\n", encoding="utf-8")
    project = output / "project"
    def command(*values):
        subprocess.run([sys.executable, str(cli), *map(str, values)], check=True)
    command("prepare", "--video", video, "--project", project, "--subtitles", srt, "--title", "合成高光演示")
    editorial = json.loads((skill / "assets" / "example-plan.json").read_text(encoding="utf-8"))
    editorial["source_sha256"] = json.loads((project / "project.json").read_text())["source"]["sha256"]
    editorial["transcript_sha256"] = hashlib.sha256((project / "transcript.json").read_bytes()).hexdigest()
    plan = project / "analysis.json"
    plan.write_text(json.dumps(editorial, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command("validate", "--project", project, "--plan", plan)
    command("render", "--project", project, "--plan", plan)
    command("montage", "--project", project, "--plan", plan, "--ids", "c01", "c02", "--output", output / "montage.mp4")
    print(f"演示预览：{project / 'clips' / 'index.html'}")


if __name__ == "__main__":
    main()
