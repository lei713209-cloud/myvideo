# voice/ — 本地 TTS 接入

本目录说明如何把本地 TTS 运行时接入流水线。**平台不内置任何模型权重**，
权重下载与合规由使用者自行完成（各模型按其官方 license）。

## 方案 A：ComfyUI 内置 CosyVoice 节点（推荐，最简单）

`core/comfy_wl_builder.py` 的 `wf_cosyvoice()` 直接构造 ComfyUI 的
CosyVoice 预置音色工作流，产物走 `/view` 下载，与图像/视频共用同一实例和队列。
适合：已有 ComfyUI 且装了 CosyVoice 自定义节点的用户。

注意：TTS 音频输出挂在结果的 `audio` 键下（不是 `images`），`wait_done()` 已处理。

## 方案 B：独立 venv + 官方运行时（隔离更干净）

官方仓库另建 venv（模型按官方文档下载），封装一个 `say.py` CLI：

```
say.py --text "..." --out line01.wav --voice default
```

要点（生产踩坑）：
1. **输出为 float32 WAV**：numpy 的 `wave` 模块读不了，
   进后处理前先 `ffmpeg -c:a pcm_s16le` 转一下。
2. **不要自写多子句 instruct**：把"语速/音色/情绪"写成一段自然语言指令传给
   instruct 参数，部分模型会把指令**逐字念出来**。生产做法：不吃 instruct，
   用运行时内置默认音色；语速靠后处理 `atempo`。
3. **同文本重录 = 确定性同结果**：修字幕/改断句可以重跑，不用换 seed。
4. Windows PowerShell 不能直接跑 `.py`，用 `& 'python.exe' 'say.py' ...` 显式调用。
5. 远端执行时设 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`，
   否则 GBK 控制台遇 emoji/特殊字符直接 UnicodeEncodeError。

远端执行模板见 `say_remote.example.ps1`。

## 方案 C：云端 TTS API

任何「按字符计费、返回音频文件」的 TTS API 都可以平替：
在 schedule.json 的 `vo` 字段指向下载好的 wav 即可，流水线其余部分不感知差异。
