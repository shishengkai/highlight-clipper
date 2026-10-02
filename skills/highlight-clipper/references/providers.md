# 下载与转写服务

本项目使用 Python 3.12 标准库发起 HTTP 请求，使用本机 FFmpeg / ffprobe 处理媒体。密钥只通过 `os.environ` 读取；不加载 `.env`、MagicDub 或 video-fetcher 的配置文件，不从命令行参数接收密钥。

## 环境变量

| 名称 | 何时需要 | 用途 |
| --- | --- | --- |
| `SNAPANY_API_KEY` | 从 YouTube 链接下载时 | 解析视频、原声音轨及已有字幕 |
| `DASHSCOPE_API_KEY` | 新建或查询 ASR 任务时 | 阿里云百炼文件转写 |
| `FAL_KEY` | 只有本地媒体且需要 ASR 上传时 | 将抽取的 WAV 上传为百炼可访问的 HTTPS 文件 |
| `DASHSCOPE_HTTP_BASE_URL` | 可选 | 默认 `https://dashscope.aliyuncs.com/api/v1`；支持北京、新加坡官方百炼域名及相应业务空间域名 |

将密钥注入运行 Codex 或脚本的进程环境。切勿将真实值写进对话、README、项目 JSON、Git 或命令行历史。已有完整字幕且足以分析时，无需付费转写；读取完成的 ASR 缓存也无需密钥。直接 HTTPS 媒体下载无需 API Key。

## 视频下载

`fetch(url, output_dir)` 接受单条 YouTube 视频地址或 HTTPS 媒体文件直链。普通网页、播放列表、HLS/DASH 清单不属于直链文件；其他平台可以先通过 video-fetcher 得到本地文件，再从本地文件建立项目。

YouTube 使用 SnapAny 的 `POST /openapi/v1/extract/post`，请求头为 Bearer 鉴权，请求体为 `{"url":"视频页面地址"}`。程序检查返回的 `site` 和视频 ID，选择最高可用清晰度，并优先选择 Original 原声音轨；没有 Original 标签时采用默认音轨。独立视频和音频下载完成后使用 FFmpeg 合并，保留原有编码。已有字幕按所选音轨的语言匹配 SRT；字幕缺失或下载失败时交由技能决定是否使用 ASR。

输出包含 `source.mp4`、可选的 `source.srt` 和 `fetch.json`。清单返回绝对 `video_path`、可选 `subtitle_path`、标题、来源和 SHA-256。直链的查询参数不会写进来源字段，完整请求地址只保存不可逆哈希以判定缓存身份。

服务器首次只返回局部 `206` 时，程序从 `Content-Range` 读取总长，并连续请求最多 1 MiB 的字节块。每块必须返回准确的起点、终点和同一总长；如果首响应带强 ETag，后续发送 `If-Range` 并检查 ETag 是否变化。拼接后再次验证总长度和文件 SHA-256，不会将局部响应冒充完整视频。

下载中断时可以重跑相同命令：未交付临时文件从零重新下载，不依据文件长度复用旧字节，也不跨次拼接不同签名链接的内容。已完成源文件通过身份和 SHA-256 检查后复用；内容发生变化时拒绝静默覆盖。下载结果需要同时有视频、音频轨，并通过完整解码检查。最终文件重命名前保存 `finalizing` 状态、来源和哈希；重命名前后中断均可恢复发布，无须重新调用解析 API。不会自动调用另一个付费代理接口。视频最高规格可能较大，下载与处理时间随原视频增加。

## 百炼 ASR

默认模型为 `qwen-audio-3.1-asr-flash-filetrans`，默认使用第一音轨并启用说话人分离。源音频会抽取为 16 kHz、单声道 PCM WAV。`language=en` 指定英语；`language=auto` 不传语言提示。说话人编号只是模型输出，不能直接当作人物身份。

若用户提供 `audio_url`，不要求 `FAL_KEY`，但 URL 必须托管**本工具从绑定源视频提取的同一份 WAV 文件**。程序仍先生成或核验本地 `asr-input.wav`，然后下载 URL 指向的文件，核对完整字节长度与 SHA-256；不一致就停止，绝不提交 ASR。另一段音频、重编码的 MP3、裁剪版音频或仅凭人工声明对应的文件均不能通过。

需要自行托管时，先运行 `audio --project 项目目录 --output 音频路径.wav` 从该项目绑定的视频提取 WAV，上传这份原样文件到自己的 HTTPS 存储，再将其 URL 传给转写命令。首次转写会重新提取并核对本地 WAV，建立它与源视频的哈希绑定。使用内容固定的存储对象，避免校验后 URL 内容变化；服务端必须能访问该地址，链接有效期应覆盖任务执行时间。URL 只在内存中使用，任务文件仅记录源与音频哈希。

提取前先保存源身份；若进程在 WAV 生成后、音频哈希保存前中断，下次会重新提取到临时目录并与现有 WAV 比对，一致后恢复记录。已有已登记音频被修改时拒绝使用。得到 ASR 任务 ID 后，后续只查询同一任务，不再上传、下载音频或新建任务。

百炼调用为：

1. `POST /services/audio/asr/transcription`，携带 `X-DashScope-Async: enable`、模型、`input.file_urls` 和 `parameters`。
2. 保存返回的 `task_id`。
3. `GET /tasks/{task_id}` 查询原任务。
4. 检查总体状态与文件子任务状态，成功后读取返回的转写 JSON。

fal 上传使用其官方 Python 客户端提供的两阶段存储协议：向 `https://rest.fal.ai/storage/upload/initiate?storage_type=gcs` 请求上传地址，然后对该地址流式 PUT 文件。API Key 仅用于第一步，不转发到文件上传地址。上传 URL 与转写下载 URL 都只在内存中使用。

本地 `asr-job.json` 包含源哈希、音频哈希、时长、模型、语言、服务地址、状态、任务 ID，以及成功后的转写和供应商用量。返回的 `raw_transcript` 保留原始文本、句子、说话人和词级时间字段，移除供应商回显的 URL 字段。`usage` 是供应商原始用量，不等同于最终账单或固定人民币价格。

### 恢复与防重复扣费

| 状态 | 含义及后续动作 |
| --- | --- |
| `new` | 还未提交 ASR；可继续音频准备或上传 |
| `submitting` | POST 前已保存状态，进程可能在等待响应时停止；不自动重交 |
| `submission_unknown` | 网络中断或响应无有效任务 ID，无法判断是否已经计费；不自动重交 |
| `submit_rejected` | 提交收到明确的 4xx（包括 429）；保存状态，不自动重交 |
| `pending` / `running` | 已有任务 ID，重跑只查询同一任务；查询 429 或网络问题不新建任务 |
| `completed` | 成功缓存，后续直接读取 |
| `failed` / `cancelled` | 终止状态，不自动新建任务 |

默认 `poll_seconds=0` 只查询一次；每次可等待最多 60 秒后返回。遇到待处理状态时，后续继续同一项目。已有任务的源、模型、语言或服务地址不同会拒绝混用。

遇到未知提交，应保留 `asr-job.json`，在百炼任务记录中核实该请求。确认找到属于同一源、模型、语言的任务后，备份状态文件，将对应 `task_id` 填回且状态设为 `pending`，再运行原命令继续查询。没有查清之前，不通过删除状态文件或创建新项目来绕过防重复提交。明确拒绝或失败后，先排查错误并确认任务终止，保留旧状态；是否重新提交按用户本次任务授权与费用约束处理，不将新任务当作恢复原任务。

程序不输出响应正文、完整签名链接和底层网络异常。带 API Key 的请求不跟随重定向；百炼服务地址只接受已支持的官方域名。项目写操作需由上层项目锁保护。

## 协议来源与验证范围

本版本开发时核对了以下官方资料与现有 video-fetcher 的媒体字段定义：

- [SnapAny Extract Post](https://platform.snapany.com/docs/extract-post)
- [百炼 Qwen-Audio / Fun-ASR 非实时语音识别 HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api)
- [fal 官方 Python 客户端存储上传实现](https://github.com/fal-ai/fal/blob/main/projects/fal_client/src/fal_client/client.py)

自动测试使用模拟 HTTP 响应，覆盖提交前状态持久化、未知提交不重交、任务恢复、429、模型/语言身份检查、`auto` 不发送语言提示且仅处理第一轨、词时间和用量保留、URL/密钥不落盘、原声音轨选择、连续 Range 校验、下载缓存、网络中断和发布前后中断恢复。音频测试覆盖错配 URL 阻止提交，并用本机 FFmpeg 实际验证提取后哈希未保存的恢复路径。测试不会产生真实 API 费用；发布前的这些协议测试不代表已使用新实现进行付费服务端到端验收。
