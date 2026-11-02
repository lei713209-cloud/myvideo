# drama/ — 多集（剧集）编排骨架

单集流水线 = `core/ + post/` 的那套；剧集层要解决的是**跨集一致性**和**批量排产**。

## 目录约定（一个剧一个目录）

```
drama_<name>/
├── project.json          # 全局：题材/风格契约/角色表/规格(画幅/响度/模型路由)
├── bible/                # 故事圣经：大纲、人物小传、每集梗概（评审后锁定）
├── ep01/
│   ├── storyboard.json   # 分镜表（见 templates/explainer_portrait.yaml 的 shots 结构）
│   ├── kf/  vo/  subs/  vids/
│   └── state.json        # 本集各环节状态（见下）
├── ep02/ ...
└── shared/               # 跨集复用资产：角色定妆图、音色样本、BGM 床、风格契约片段
```

## state.json 状态机（每集）

```
draft -> script_locked -> kf_done -> kf_approved -> vid_done
      -> qc_passed -> vo_done -> mixed -> final -> published
```

规则：
* 每个状态只在**闸门通过后**推进；闸门失败退回上一个可修复状态（如 qc_passed→kf_done 重生成单镜）。
* 断点续跑：重跑时扫描 state.json，只处理未达标环节；产物存在且过闸 = 跳过（幂等）。
* 人工审批点：script_locked / kf_approved / final 三处必须人点头，其余自动。

## 跨集一致性

* **风格契约**写在 project.json，全剧所有关键帧/视频 prompt 强制拼接同一段契约文本。
* **角色资产**放 shared/：定妆图作为 i2v 参考、LoRA/音色样本按角色挂接；改设定的唯一入口是 shared/ + bible/。
* **规格冻结**：画幅、帧率、响度目标在 project.json 定死，单集不得私改。

## 参考实现

* `audio_contract.py` — 旁白音频的公共契约：静音压缩、逐句时长与响度指标产出，供排期器消费。
* `grid_contract.py` — 选优宫格生成：每镜 N 候选拼一张图，交 vision 评审或人工圈选，产出 chosen.json。

批量排产（今晚渲 3 集）= 对每集依次调 core/batch_runner.py + post/assemble.py，
队列深度与 VRAM 回收见 core/comfy_wl_builder.py 的 queue_state()/free_models()。
