# 编辑计划与项目格式

格式版本为 `schema_version: 1`。使用 `plan-template` 从实际项目生成身份字段，然后由 Codex 填写内容。参考 [六句虚构问答示例](../assets/example-plan.json)；示例中的两个全零哈希只是占位，不能直接用于真实视频。

## 来源与转写

`project.json` 绑定实际媒体路径、SHA-256、毫秒时长、尺寸、标题、来源 URL 与语言。URL 中可能包含凭据的查询部分不写入来源说明；YouTube 保留视频 ID。

`transcript.json` 保存同一源视频的句级和可用词级时间，句号从 1 连续增长。支持 SRT、VTT、规范化 JSON，以及单轨 DashScope JSON。每句包括 `id`、`start_ms`、`end_ms`、`text`、可选 `speaker_id`、`words`。词含 `start_ms/end_ms/text`。说话人标签是自动标签，不是已确认身份。

`precision` 为 `subtitle`、`sentence` 或 `word`。字幕具有毫秒格式不代表逐帧准确。字幕重叠会保留，不能靠排序或去重悄悄改变原文。更换转写会改变文件哈希，因此旧计划必须重新核对。`import-transcript --replace` 会先保留旧稿。

## analysis.json

- `source_sha256`、`transcript_sha256` 必须对应当前项目。不得为跳过不匹配而只换哈希、不复查内容。
- `source_structure`：演讲、访谈、多层评论、预告等结构及区别。
- `overview`：帮助首次观看者理解内容的短导览。
- `passes`：`stories_examples`、`ideas_conflicts`、`language_interaction`、`omission_review` 四轮检查的具体发现和遗漏复查说明。记录实际做过的工作。
- `topics`：按顺序覆盖全部句子的语义话题表。每项包括 `id/title/start_id/end_id/source_layer/candidate_ids/audit_note`。同一句必须恰好归入一个话题；无候选的话题也要保留并说明复查结果。
- `first_watch`：建议先看的候选 ID，按观看顺序排列。不改变候选库或默认渲染范围。

每个 `candidates` 元素包含：

| 字段 | 含义 |
| --- | --- |
| `id` | 1—80 个字母、数字、下划线或连字符，首字符为字母或数字；用于稳定引用与文件目录 |
| `title` | 中文拟题，忠实于该片段，不扩大原话 |
| `priority` | `优先` 或 `备选`，不等于正确与错误 |
| `types` | 一种或多种观看价值，非空字符串列表 |
| `reason` | 具体说明精彩在哪里，不能只写“有爆点” |
| `source_layer` | 实际发言来源，例如嘉宾回答、主持人提问、上传者评论；不冒认人物 |
| `context_note` | 前提、问题、解释、限定、结尾和该长度版本的取舍 |
| `range` | 连续切片的句/词定位 |
| `core` | 核心亮点的句/词定位，必须完整位于切片中 |
| `question_ids` | 相关问题的句号，可跨段关联；超出片段时应在上下文说明中解释 |
| `variant_of` | `null` 或主候选 ID，用于同一亮点的长度比较，不形成链或环 |
| `review` | `attribution/context/boundary` 三项，记录实际复核依据、尚未完成的检查与限度 |

`range/core` 都用 `{ "start_id": 3, "end_id": 7 }`，可附 `start_word/end_word`（各自在首/末句中的一基词序号）。先运行 `read --words` 获取实际词号。程序将编号映射到真实时间，避免模型凭记忆填写毫秒。不能跨过中间内容假装连续；跨段关系放在说明里，若需集锦则明确列出各段。

确有回看依据的人工边界修正，可加 `boundary_override: {"start_ms": ..., "end_ms": ..., "reason": "...", "evidence": "..."}`。不得编造听审或帧证据。修正后仍须包含核心，且不能超出源视频。边界穿过只有句级时间的字幕时，程序会拒绝伪造半句字幕；应改用合适的句边界，或补充词级转写后复核。

## 输出和检查范围

`selection.json` 是由计划编译得到的真实切点、核心范围、问题包含情况、实际时间重叠及结构检查结果。`declared_sentence_ids` 保留计划声明的句号，`sentence_ids` 则按最终切点列出实际相交的全部句号（包括人工扩展和重叠字幕）。程序不按评分删掉条目，没有默认 Top N。

各段保存为 `<候选ID>/clip.mp4`、`clip.auto.srt`、`info.json`。同一来源和切点的已完成结果先核验哈希再复用；改变源、转写或切点时使用新输出目录，保留比较版本。

`delivery.json`、`index.html`、`README.md` 保留完整候选，即使只渲染部分 ID；预览相对路径可随整个输出目录搬移。`feedback.csv` 支持用户填写；`feedback` 命令将反馈追加到项目 `feedback.json`，重新 `report` 后反映到预览。反馈通过 `candidate_key` 绑定源、转写、ID、切点和核心等信息，新片段复用同一 ID 不会继承旧评分。若 CSV 和命令在两次报告间分别修改且发生冲突，保留 CSV 内容，将另一个意见存入 `feedback-conflicts.json`，供用户复核；历史事件不删除。`feedback --plan` 可指定实际评价的计划。`magicdub-handoff.json` 是交接清单，不是 MagicDub 原生批量配置。

`montage` 只按明确 ID 顺序合并已检查的切片，另存 `.manifest.json` 标记各段在集锦和原视频中的位置。时间重叠必须通过明确 `--allow-overlap` 允许。其隐藏暂存目录保存任务身份与完成哈希，可在发布视频和清单之间中断后恢复；相同输入重复执行复用已经核验的成品，不覆盖其他内容。合并和文件检查不代替完整观看、语义审查或最终发布决定。

源哈希、覆盖结构、解码、尺寸、时长和字幕文件检查都是客观文件证据。它们不能证明原文无误、高光没有漏选、说话人身份已确认、完整听审已完成或观众一定喜欢。
