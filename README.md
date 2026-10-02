# highlight-clipper

把长视频中的好观点、故事、金句和精彩互动找出来，保留必要上下文，再切成可供观看和比较的短片。

这是一个 **Codex skill**：Codex 阅读与判断内容，Python 3.12 程序负责素材、时间轴、切片和文件检查。适合演讲、访谈、播客，以及夹有原片摘录和评论旁白的视频。

## 它怎样选片

1. 阅读全片，建立覆盖全部字幕的主题地图，识别提问、回答、例子、转折和来源层。
2. 分别从故事与案例、观点与冲突、语言与互动三个角度重新检查原文。
3. 合并候选，补查没有选入的区域；不预设条数，备选也保留。
4. 判断每个亮点需要多少前因、问题和后续回应；有必要时提供长短版本。
5. 根据当前素材的句级或词级时间切片，输出预览、字幕、选片理由和反馈入口。

任何一种观看价值特别突出，就可以进入候选。外部事实、数据准确性和信源可信度不用于淘汰或降级候选；仍要忠于原话、人物归属、适用条件和玩笑后的澄清。完整规则见 [编辑标准](skills/highlight-clipper/references/editorial.md)。

## 运行条件

- Python **3.12**。
- 系统可执行的 `ffmpeg`、`ffprobe`。
- 已有视频和字幕时，可以完全在本机处理。
- 下载和云端 ASR 按需使用外部服务，API Key 仅从当前进程的环境变量读取。

| 功能 | 环境变量 | 何时需要 |
| --- | --- | --- |
| 通过 SnapAny 获取 YouTube 素材 | `SNAPANY_API_KEY` | 使用 `fetch` 下载 YouTube 时 |
| DashScope 云端 ASR | `DASHSCOPE_API_KEY` | 使用 `transcribe` 时 |
| 上传本机音频供 ASR 读取 | `FAL_KEY` | 未提供托管同一份提取 WAV 的 `--audio-url` 时 |

Python 程序不需要额外的 pip 运行依赖，也不要求安装 video-fetcher 或 MagicDub。Codex 使用当前会话模型完成选片，无需额外配置 LLM API Key。ASR、下载和上传费用由对应服务计收，程序不会把它们视为 Codex 订阅用量。

## 先查看，再安装

技能放在 `skills/highlight-clipper/` 中。克隆仓库不会把它安装到 Codex 的技能目录。

先检查本机依赖：

```bash
python3.12 skills/highlight-clipper/scripts/highlight_clipper.py doctor
```

可以先阅读 [SKILL.md](skills/highlight-clipper/SKILL.md)、[编辑标准](skills/highlight-clipper/references/editorial.md) 和 [工作流](skills/highlight-clipper/references/workflow.md)，检查选片规则是否符合预期。也可以不安装技能，直接使用程序准备素材和渲染已经写好的选片计划。

先看完整的本地演示，无需联网或 API Key：

```bash
python3.12 examples/create_demo.py --output projects/demo
```

它生成 13 秒彩色测试视频、测试音和虚构的六句字幕，使用随附示例计划完成校验、切片、报告和集锦。打开 `projects/demo/project/clips/index.html` 查看，集锦位于 `projects/demo/montage.mp4`。这是文件处理与交付形式的演示，测试音不包含字幕中的真实讲话，也不代表模型选片或听审效果。演示目录必须是新目录，已有目录不会被覆盖。

从仓库根目录运行本地测试，无需 API Key：

```bash
python3.12 -m unittest discover -s tests -v
```

测试包含真实 FFmpeg 处理合成音视频，以及外部 API 的模拟协议、状态恢复和凭据处理检查。另已复用既有真实访谈和词级转写导出一条预览，验证历史数据兼容性。详情见 [验证记录](docs/验证记录.md)。当前未完成本版本真实付费下载、上传和 ASR 服务的端到端验证；模拟测试通过不能替代实际服务验收。测试也不评价 Codex 的主观选片质量。

确认需要安装时，将整个 `skills/highlight-clipper/` 文件夹复制到 Codex 的用户技能目录 `~/.agents/skills/highlight-clipper/`。其中 `scripts/`、`references/`、`assets/` 和 `agents/` 都需要保留。如果那里已有同名技能，先检查旧版本及其中的修改，再决定如何更新。目录格式依据 [Codex 官方技能文档](https://learn.chatgpt.com/docs/build-skills)。

安装后可以这样提出任务：

> 使用 $highlight-clipper 分析这个视频，充分发现候选，保留备选和必要上下文。先给我内容导览，再切出全部候选供我比较：视频链接或本机路径。

> 使用 $highlight-clipper 继续这个项目。第 03 段选点很好，但缺少采访者的问题，请补一个完整问答版，保留原版用于比较。

## 从本机视频开始

以下命令从仓库根目录运行。把路径改成实际素材和工作目录；输出工作目录建议放在仓库外，避免将媒体或个人内容提交到公共仓库。

```bash
python3.12 skills/highlight-clipper/scripts/highlight_clipper.py prepare \
  --video /path/to/source.mp4 \
  --subtitles /path/to/source.srt \
  --project /path/to/highlight-project \
  --language en \
  --title "示例访谈"

python3.12 skills/highlight-clipper/scripts/highlight_clipper.py read \
  --project /path/to/highlight-project

python3.12 skills/highlight-clipper/scripts/highlight_clipper.py plan-template \
  --project /path/to/highlight-project \
  --output /path/to/highlight-project/analysis.json
```

随后由 Codex 按技能流程阅读全文并填写计划。模板自身不是选片结果，必须填写主题、候选、复核依据和首次观看入口。

```bash
python3.12 skills/highlight-clipper/scripts/highlight_clipper.py validate \
  --project /path/to/highlight-project \
  --plan /path/to/highlight-project/analysis.json

python3.12 skills/highlight-clipper/scripts/highlight_clipper.py render \
  --project /path/to/highlight-project \
  --plan /path/to/highlight-project/analysis.json
```

`render` 会同时生成报告。随后编辑说明或反馈变化时，可单独更新报告：

```bash
python3.12 skills/highlight-clipper/scripts/highlight_clipper.py report \
  --project /path/to/highlight-project \
  --plan /path/to/highlight-project/analysis.json
```

没有字幕时可以省略 `--subtitles`，再导入转写或运行 `transcribe`。下载、转写、局部回看和继续已有项目的操作见 [工作流与命令](skills/highlight-clipper/references/workflow.md)。

## 你会得到什么

- 原片内容导览和较短的首次观看入口。
- 全部候选的标题、观看价值、原片起止时间、来源层、上下文说明和优先/备选建议。
- 各候选的视频片段和对应字幕；长短版本与重复关系明确标注。
- 全片主题覆盖及未单列区域的检查说明。
- 文件检查结果和仍需人工复核的内容。
- “必选 / 可选 / 不选 / 选点对但范围不对”反馈记录。

默认结果位于项目的 `clips/`。打开 `index.html` 观看；每条候选子目录包含 `clip.mp4`、`clip.auto.srt` 和 `info.json`。还会生成文字报告、结构化选片与验收记录、反馈表，以及供后续译制使用的 MagicDub 交接清单。

反馈绑定具体的源片、转写和切片版本，修改范围后不会自动套用旧评分。既手填反馈表又通过命令提交且意见冲突时，保留手填内容，将另一条意见保存到冲突记录供复核。

同主题的不同好内容可以分别保留。长短变体和相互包含的候选用于比较，不应直接全部拼成集锦。每条候选使用连续的原片区间；跨时段的补充回答可以关联说明，不能伪装成连续对话。

需要集锦时，`montage` 按明确的候选顺序拼接，并保存各段在集锦和源片中的位置。默认拒绝相互重叠的片段；操作示例见 [制作集锦](skills/highlight-clipper/references/workflow.md#6-制作集锦)。

这里的“验证通过”表示程序能够证明的结构和文件检查通过。它不等于已经完整听审、逐帧验收、翻译校对或证明视频会受欢迎。没有完整人工参考清单时，也不声称已经找齐全片所有亮点。

## 项目结构

```text
skills/highlight-clipper/
├── SKILL.md                  # Codex 的入口与执行规则
├── agents/openai.yaml        # Codex 展示信息
├── assets/example-plan.json # 六句虚构问答的计划示例
├── references/
│   ├── editorial.md          # 编辑标准、上下文与反馈
│   ├── workflow.md           # 执行步骤与命令
│   ├── schema.md             # 项目、编辑计划与输出格式
│   └── providers.md          # API 配置、协议与恢复
└── scripts/
    ├── highlight_clipper.py  # Python 3.12 命令入口
    └── hc/                  # 素材、时间轴与渲染实现
```

技能目录自包含，运行时通过实际技能位置寻找脚本。项目和素材路径由用户提供，不依赖某一台机器的目录。未来可以复用程序适配其他宿主，当前交付入口是 Codex skill。

## 许可证

[MIT](LICENSE)。
