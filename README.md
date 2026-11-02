# myvideo — 本地优先的 AI 视频/图片/剧集生产流水线

**Local-first AI video/image/drama production pipeline.** 一条从「文稿 → 分镜 → 关键帧 → 图生视频 → 质检 → 配音 → 字幕 → 混音 → 成片」的可复现流水线，跑在你自己的 GPU 上（一张 RTX 4090 即可），不依赖任何特定云服务。

**发布渠道**：本仓库与 https://github.com/edmund-xl/myvideo 为同一维护方的双发布渠道，内容同源、提交历史各自独立。

## 它解决什么问题

现成的"AI 一键出片"工具输出质量参差、不可控；而 ComfyUI 强大但对内容创作者门槛太高。myvideo 填的是中间的空白——**把经过生产验证的工序与质量闸门代码化**：

- **分镜驱动**：storyboard/schedule JSON 是唯一的真相源，所有产物可断点续跑、可审计。
- **质量闸门即代码**：关键帧 vision 评审不过 8 分不进图生视频；旁白-画面刚性对齐（超出镜头时长直接报错，不静默错位）；交付自动过 EBU R128 响度标准（I=-16 LUFS / TP≤-1.5）。
- **本地优先，云端可插拔**：LLM/VLM/TTS/视频生成都通过统一路由层配置，本机的 Ollama/vLLM/ComfyUI 和任何 OpenAI 兼容 API 一视同仁。
- **多集一致性**：剧集层的风格契约（style contract）、角色定妆资产、状态机排产，保证 20 集看起来是同一部剧。

## 目录结构

```
core/       ComfyUI 工作流构造器 + 批量任务 runner（关键帧/i2v/TTS/资源回收）
post/       后期四件套：静音压缩、PIL 字幕、numpy BGM 合成、成片总装(align+mix+loudnorm)
voice/      本地 TTS 接入指南（ComfyUI 节点 / 独立 venv / 云 API 三种方案）
drama/      多剧集编排：project.json schema、每集状态机、跨集一致性契约
templates/  开箱模板（竖屏科普口播片等）
config/     全局配置示例（GPU 端点、模型文件名、交付规格）
docs/       需求与设计文档
```

## 快速开始

1. 准备一台跑着 [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 的机器（建议 ≥16GB VRAM；24GB 全功能）。
2. `cp config/config.example.yaml config/config.yaml`，填上你的 ComfyUI 地址与模型文件名。
3. 复制 `templates/explainer_portrait.yaml`，改成你的选题与旁白。
4. 用 `core/` 提交关键帧与视频任务，`post/` 完成对齐、混音与封装。
   （每个脚本 `--help` / 文件头注释即是用法；端到端示例见 docs/。）

## 已验证的硬性纪律（写进了代码，不是写进愿望）

- 关键帧按 4MP 母版生成，Lanczos 降采样后进 i2v，禁止全尺寸直灌。
- 视频帧栅格 (F-5)%17==0，档位 124/192 帧，超限需显式授权。
- 远端 Windows：ASCII 路径 + `PYTHONUTF8=1`；SSH 隧道目标写 `127.0.0.1` 不写 `localhost`。
- TTS 不吃自写多子句 instruct（会被逐字念出），语速靠 `atempo` 后处理。
- 24GB VRAM 串行排队：LLM/图/视频不能同时驻留，提交前查 `vram_free_gb()` 并按需 `free_models()`。

## 状态

当前为**核心工序库**（scripts + contracts + docs），Web UI 与托管计费层在规划中，
见 `docs/REQUIREMENTS.md`。欢迎评审、拍砖、PR。

## License

Apache-2.0
