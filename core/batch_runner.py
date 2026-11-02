# Copyright 2026 lei713209-cloud
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""通用批量 runner 模板（关键帧 / 视频段 / 配音，4090 ComfyUI，经 comfy_wl_builder）。
纪律：每任务 try/except 不中断整批；state JSON 落盘，重跑自动跳过 done；逐任务打耗时。
用法：把本文件和 comfy_wl_builder.py 放在同一目录（技能 scripts/ 或 $scratch 均可），
填 PROJ/OUTDIR/ORIENT/JOBS，后台跑 + notify "BATCH_DONE"。
JOBS 每项 kind=kf|vid|tts：
  kf : {"kind":"kf","tag":"kf1","prompt":关键帧提示词,"seed":77001}                      → 4MP 母版 + _deliv 送视频版
  vid: {"kind":"vid","tag":"short1","prompt":英文视频提示词,"seed":88001,"length":124,"kf_local":"kf1_deliv_00002_.png"}
       （可加 "kf_last_local" 做 i2v_fl；length 默认档 124/192，243 起需 APPROVED_LIMIT_S）
  tts: {"kind":"tts","tag":"vo_s1","text":"台词原文","voice":"中文女","speed":0.95,"seed":99001}
修好参数后只清空 state 里的 fail 列表重启 runner，禁止逐镜手工重发。
"""
import sys, json, time, os, hashlib, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # builder sits next to this file
import comfy_wl_builder as B

PROJ = "REPLACE_EN"          # 4090侧英文短名
OUTDIR = os.path.expanduser("~/Desktop/REPLACE/kf")   # 拉回落点（kf / shots / audio 各跑一批时分别指过去）
ORIENT = "landscape"         # "landscape"=1344×768（母版2688×1536） / "portrait"=768×1344（母版1536×2688）
APPROVED_LIMIT_S = None      # 用户批准并落盘的单段上限秒数（如 10.13）；未批准保持 None
STATE = os.path.expanduser("~/.hermes/cache/scratch/%s_state.json" % PROJ)
JOBS = []

if ORIENT not in B.CANVAS:
    sys.exit("ORIENT must be landscape or portrait")
B.PROJ = PROJ
W, H = B.CANVAS[ORIENT]

state = json.load(open(STATE)) if os.path.exists(STATE) else {"done": {}, "fail": {}, "uploaded": {}, "timing": []}
save = lambda: json.dump(state, open(STATE, "w"), ensure_ascii=False, indent=1)


def up(local, *, content_bound=False, expected_sha=None):
    path = os.path.join(OUTDIR, local)
    # Explicit grid jobs bind uploads to actual bytes. Legacy jobs retain their
    # historical cache keys so this optional experiment cannot alter old runs.
    cache_key = local
    content = None
    if content_bound:
        with open(path, "rb") as source:
            content = source.read()
        content_sha = hashlib.sha256(content).hexdigest()
        if expected_sha is not None and content_sha != expected_sha:
            raise ValueError("Grid tile changed after provenance validation")
        cache_key = "sha256:" + content_sha + ":" + local
    if cache_key not in state["uploaded"]:
        if content_bound:
            # Upload the exact verified bytes, not a live project path that a
            # concurrent task may overwrite after hashing. Preserve the basename
            # expected by ComfyUI and remove the private copy on success/failure.
            with tempfile.TemporaryDirectory(prefix="grid-upload-") as scratch:
                snapshot = os.path.join(scratch, os.path.basename(local))
                with open(snapshot, "wb") as target:
                    target.write(content)
                os.chmod(snapshot, 0o400)
                state["uploaded"][cache_key] = B.upload(snapshot)
        else:
            state["uploaded"][cache_key] = B.upload(path)
        print("UPLOADED", local, "->", state["uploaded"][cache_key], flush=True)
        save()
    return state["uploaded"][cache_key]


for j in JOBS:
    key = j["tag"] + "_s" + str(j["seed"])
    grid_signature = None
    grid_evidence = None
    t0 = time.time()
    try:
        if j.get("keyframe_sources") is not None:
            from grid_contract import validate_job_sources
            grid_evidence = validate_job_sources(j, OUTDIR, [j["kf_local"]] + ([j["kf_last_local"]] if j.get("kf_last_local") else []))
            grid_signature = hashlib.sha256(json.dumps(grid_evidence, sort_keys=True).encode()).hexdigest()
            if key in state["done"] and state["done"][key].get("grid_signature") != grid_signature:
                raise ValueError("Grid sources changed; keep prior output and publish a new task version")
        if key in state["done"]:
            print("SKIP", key, flush=True); continue
        kind = j["kind"]
        if kind == "kf":
            wf = B.wf_qwen_t2i_kf2k(j["prompt"], j["tag"], j["seed"], orient=ORIENT, neg=j.get("neg", B.NEG))
        elif kind == "vid":
            first_sha = (grid_evidence[0] or {}).get("tile_sha256") if grid_evidence else None
            kfs = [up(j["kf_local"], content_bound=grid_signature is not None, expected_sha=first_sha)]
            if j.get("kf_last_local"):
                last_sha = (grid_evidence[1] or {}).get("tile_sha256") if grid_evidence else None
                kfs.append(up(j["kf_last_local"], content_bound=grid_signature is not None, expected_sha=last_sha))
            wf = B.wf_fasth3_i2v(kfs, j["prompt"], j["length"], j["tag"], j["seed"],
                                 width=W, height=H, approved_limit_s=APPROVED_LIMIT_S)
        elif kind == "tts":
            wf = B.wf_cosyvoice(j["text"], j["voice"], j["tag"], j["seed"], speed=j.get("speed", 0.95))
        else:
            raise ValueError("unknown kind " + str(kind))
        pid = B.submit(wf)
        print("SUBMIT", key, "pid=" + pid, flush=True)
        files = B.wait_done(pid, timeout=1800)
        if not files or files[0] in ("ERROR", "TIMEOUT"):
            raise RuntimeError(str(files)[:300])
        if kind == "kf":
            picked = files
        elif kind == "vid":
            picked = [(s, f) for s, f in files if f.endswith(".mp4")]
        else:
            picked = [(s, f) for s, f in files if f.rsplit(".", 1)[-1].lower() in ("flac", "wav", "mp3")]
        if not picked:
            raise RuntimeError("no expected outputs: " + json.dumps(files)[:200])
        for sub, fn in picked:
            dest = os.path.join(OUTDIR, fn)
            B.pull(sub, fn, dest)
            print("GOT", fn, "(%dKB)" % (os.path.getsize(dest) // 1024), flush=True)
        dt = time.time() - t0
        state["timing"].append([key, round(dt, 1)])
        state["done"][key] = {"pid": pid, "secs": round(dt, 1), "files": [f for _, f in picked]}
        if grid_signature is not None:
            state["done"][key]["grid_signature"] = grid_signature
        state["fail"].pop(key, None)
    except Exception as e:
        state["fail"][key] = str(e)[:300]
        print("FAIL", key, e, flush=True)
    save()

print("BATCH_DONE ok=%d fail=%d" % (len(state["done"]), len(state["fail"])), flush=True)
