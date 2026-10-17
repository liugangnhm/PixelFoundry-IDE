# Agnes AI 内置接入 / Built-in integration

在「设置 → API 配置」的文本、图片、视频三个页签中，分别新建配置，选择
**Agnes AI** 服务商预设，填写自己的 API Key，保存并设为该类型默认配置。
无需填写 JSON 模板或手动检测字段。已有配置不会被自动覆盖。

In Settings → API configuration, create a configuration for each API kind,
select the **Agnes AI** preset, enter your own key, save and set it as the
default for that kind. No request templates or field detection are required.

| 类型 / Kind | 默认模型 / Default model | 请求 / Request |
| --- | --- | --- |
| 文本 / Chat | `agnes-3.0-flash` | `POST /v1/chat/completions` |
| 文生图、图生图 / Images | `agnes-image-2.5-flash` | `POST /v1/images/generations` |
| 首帧动画 / Video | `agnes-video-2.5-flash` | `POST /v1/videos` |

Base URL 默认 `https://apihub.agnes-ai.com/v1`，鉴权使用 Bearer。
模型可通过「查询模型」更换，实际权限以账号为准。

图像适配区分文生图 Base64 返回（`return_base64`）与图生图
（`extra_body.image` 数组及 `extra_body.response_format`）。本地参考图直接转为
Data URI 发送给 Agnes。通用 OpenAI 的 `steps`、`n` 和顶层 `response_format`
不会发送到 Agnes 图像接口。

视频默认使用 `keyframe` 模式，首帧使用本地 PNG 的 Data URI；如需指定公网首帧地址，
可在高级选项中设置 `image_url`。请求携带 `first_frame`，开启首尾帧一致时也发送
`last_frame`。支持 `reference` 和 `text` 模式；`reference` 将首帧放入 `images` 数组。
不会把图片上传到额外的第三方图床。

视频将帧数 / 帧率换算为时长并限制在 4–12 秒，`seconds` 使用字符串。
Flash 仅支持 `720P`；切换 `agnes-video-2.5` 后可选 `1080P`、`1K`、`2K`。
默认画面比例 `1:1`，可在高级选项更改。服务商生成的视频帧率由服务端决定，
应用原有拆帧与导出流程负责得到需要的帧序列。

轮询地址自动构造为同一服务根路径的
`/agnesapi?video_id=...&model_name=...`，优先取提交响应的 `video_id`，
即使它与 `id` / `task_id` 不同。以公开的 `status` 判断完成，读取顶层 `url`；
忽略可能滞后的 `internal_status`。预设最多轮询 180 次，间隔 5 秒，可自行调整。
完成响应没有视频地址时会报错，不会显示生成成功。
状态查询遇到限流或网络异常时会增加等待时间，并遵循服务端数字形式的
`Retry-After`；鉴权失败或无效的视频 ID 会直接报告错误。

The adapters use Agnes-specific image parameters and asynchronous video
retrieval. Local inputs are sent directly to Agnes as Data URIs. Videos use
`video_id` plus `model_name` for polling, accept only completed results, and
flow through the existing frame extraction and export pipeline. Custom
endpoints and request templates remain available for alternate gateways.

## 验证 / Validation

离线测试 / Offline tests:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_agnes_api.py -v
```

真实接口测试 / Opt-in live test (uses API quota):

1. 在当前进程的环境变量中设置 `AGNES_API_KEY`，不要写入源码或提交文件。
2. 执行 `.venv/Scripts/python.exe scripts/smoke_agnes.py --video`。
3. 检查 `.tmp/agnes-smoke/report.json`、生成的图片和 `animation.mp4`。

脚本通过内置预设及客户端工厂依次测试文本、文生图、图生图、首帧视频，并下载视频
检查实际拆帧。可用 `--video-id VIDEO_ID` 复用已提交的视频任务。
输出目录已被 Git 忽略。API Key 沿用应用现有 Fernet 加密存储机制。

## 官方接口依据 / Official contracts

核对日期 / Checked: 2026-10-18.

本地真实测试已通过文本、文生图、图生图及 Data URI 首帧视频生成。
下载的 H.264/AAC 视频可解码为 107 帧，24 FPS，704×704，容器时长约 4.46 秒
（请求 4 秒、720P、1:1；实际输出以文件为准）。测试 Key 未写入仓库。
16 项专用测试通过，覆盖请求格式、配置页保存、加密、轮询 ID、失败状态、
缺失结果及 429 退避。全量回归在此 Windows 环境出现配置文件替换的
`PermissionError: [WinError 5]`，相同错误已在未修改的上游代码复现；
失败项随后重跑通过，全量首轮并非全部通过。

- [Agnes 3.0 Flash](https://agnes-ai.com/en/docs/agnes-30-flash)
- [Agnes Image 2.5 Flash](https://agnes-ai.com/en/docs/agnes-image-25-flash)
- [Agnes Video 2.5 Flash](https://agnes-ai.com/en/docs/agnes-video-25-flash)
- [Agnes Video 2.5](https://agnes-ai.com/en/docs/agnes-video-25)

官网当前文档及实时模型列表优先于旧版 GitHub 示例；旧视频模型
`agnes-video-v2.0` 已在官网标为退役，故未作为默认值。
