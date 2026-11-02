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

# ComfyUI API workflow builders (validated: Qwen-Image t2i/EditPlus kf + FastH3 i2v + CosyVoice TTS).
# Usage: keep this file and batch_runner.py in the same folder, edit PROJ below (or see config/config.example.yaml),
# point BASE at your ComfyUI endpoint and submit.
#
# Node-graph recovery source: rebuild any validated workflow programmatically without relying on /history.
#
# Restructure notes: node graphs are unchanged from the validated versions; added guards and helpers:
#   - wf_fasth3_i2v(): checks frame grid (F-5)%17==0, default tiers 124/192, longer tiers only with approved_limit_s,
#     canvas 1344x768 / 768x1344 (draft=True to bypass for layout tests), kfs must be a list.
#   - wait_done(): collects images / audio / videos / gifs outputs (TTS nodes land under 'audio').
#   - pull(): URL-encodes filename/subfolder, downloads to an ASCII temp path, then moves (non-ASCII dest ok).
#   - upload(): ASCII-safe remote name with project prefix (Windows-side paths must stay English).
#   - system_stats() / vram_free_gb() / free_models() / queue_state() / interrupt().
#   - wf_cosyvoice(): CosyVoice pre-trained voice graph (end-to-end tested).
#   - kf builders accept orient="landscape"|"portrait" and an optional neg prompt.
import json, urllib.request, urllib.error, urllib.parse, os, shutil, tempfile, time, mimetypes, re, uuid, hashlib

# ComfyUI endpoint. When accessing a remote machine through an SSH tunnel, forward with an explicit
# IPv4 target (`-L <port>:127.0.0.1:8188`), never `localhost`: sshd may resolve `localhost` to ::1
# after reboot/network changes while ComfyUI listens IPv4-only -> tunnel binds fine but every request
# dies RemoteDisconnected. Symptom: ssh alive, port LISTEN, curl 000.
BASE = os.environ.get("MYVIDEO_COMFY_BASE", "http://127.0.0.1:8188")
# CANVAS POLICY: keyframes GENERATE at 2x (4MP master), then Lanczos-downscale to the video canvas
# before i2v — never feed full-size to i2v. Save BOTH (master = edit/archive source; _deliv = i2v input).
PROJ = "myvideo_project"          # server-side folder: ASCII only
PROJ_MAC = os.path.expanduser("~/Desktop/myvideo_project")   # client side may be non-ASCII

CANVAS = {"landscape": (1344, 768), "portrait": (768, 1344)}      # FastH3 max native canvas
MASTER = {"landscape": (2688, 1536), "portrait": (1536, 2688)}    # 4MP keyframe master = 2x canvas
DEFAULT_FRAMES = (124, 192)                                         # production tiers (5.17s / 8.00s)
MAX_FRAMES = 345                                                     # 14.38s; 362 exceeds official 15s


# ---------------------------------------------------------------- HTTP helpers
def _post(path, data=None, headers=None):
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers or {})
    try:
        raw = urllib.request.urlopen(req, timeout=60).read()
        r = json.loads(raw) if raw.strip() else {}
        if isinstance(r, dict) and r.get("node_errors"):
            raise RuntimeError(json.dumps(r["node_errors"], ensure_ascii=False)[:900])
        return r
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode(errors="replace")[:900])


def _get(path, timeout=15):
    return json.load(urllib.request.urlopen(f"{BASE}{path}", timeout=timeout))


def submit(wf):
    """Returns the prompt_id STRING (not a dict).
    json.dumps keeps ensure_ascii=True on purpose: CJK request bodies sometimes hang over the tunnel."""
    return _post("/prompt", json.dumps({"prompt": wf}).encode(),
                 {"Content-Type": "application/json"})["prompt_id"]


def history(pid):
    return _get(f"/history/{pid}")


def system_stats():
    return _get("/system_stats")


def vram_free_gb():
    """Key is vram_free / vram_total (NOT memory_free)."""
    d = system_stats()["devices"][0]
    return round(d["vram_free"] / 1024 ** 3, 1)


def free_models():
    """Unload resident models (e.g. Qwen image model before an H3 batch, else H3 OOMs)."""
    return _post("/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                 {"Content-Type": "application/json"})


def queue_state():
    q = _get("/queue")
    return {"running": len(q.get("queue_running", [])), "pending": len(q.get("queue_pending", [])), "raw": q}


def interrupt():
    """Killing a local poller does NOT cancel the 4090 job; this does."""
    return _post("/interrupt", b"", {"Content-Type": "application/json"})


OUTPUT_KEYS = ("images", "audio", "videos", "gifs")


def wait_done(pid, timeout=900, poll=8):
    """Returns [(subfolder, filename)] on success, ('ERROR'|'TIMEOUT', ...) otherwise.
    - SaveVideo lands under outputs[*]['images'] with .mp4 names (not 'videos'); SaveAudio under 'audio'.
    - /history/<pid> may be empty for a few seconds after the job finishes: keep polling until
      status=success AND outputs carry files (never index h[pid] blindly)."""
    dl = time.time() + timeout
    while time.time() < dl:
        try:
            h = history(pid)
            if pid in h:
                st = h[pid].get("status", {}).get("status_str")
                files = []
                for o in h[pid].get("outputs", {}).values():
                    for key in OUTPUT_KEYS:
                        for f in o.get(key, []) or []:
                            if isinstance(f, dict) and f.get("filename"):
                                files.append((f.get("subfolder", ""), f["filename"]))
                if st == "success" and files:
                    return files
                if st == "error":
                    return ("ERROR", json.dumps(h[pid].get("status", {}), ensure_ascii=False)[:800])
        except Exception as e:
            print("poll err", e, flush=True)
        time.sleep(poll)
    return ("TIMEOUT", pid)


def pull(sub, fn, dest):
    """Download one output. subfolder/filename must come from history verbatim (never hardcode 'output').
    Writes to an ASCII temp path first (curl/urllib to Chinese paths failed with rc 23), then moves."""
    q = urllib.parse.urlencode({"filename": fn, "subfolder": sub, "type": "output"})
    fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(fn)[1] or ".bin")
    os.close(fd)
    with urllib.request.urlopen(f"{BASE}/view?{q}", timeout=600) as r, open(tmp, "wb") as w:
        shutil.copyfileobj(r, w)
    if os.path.getsize(tmp) == 0:
        os.remove(tmp)
        raise RuntimeError(f"pulled 0 bytes: {sub}/{fn}")
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    shutil.move(tmp, dest)
    return dest


def _ascii_name(fn):
    stem, ext = os.path.splitext(os.path.basename(fn))
    if not re.search(r"[^A-Za-z0-9._-]", stem):
        return f"{stem}{ext.lower() or '.png'}"
    # non-ASCII / spaces removed: add a short hash so different Chinese names stay distinct
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")
    safe = f"{safe or 'img'}_{hashlib.md5(stem.encode()).hexdigest()[:8]}"
    return f"{safe}{ext.lower() or '.png'}"


def upload(path, name=None):
    """POST /upload/image; MUST use the returned name for LoadImage (same-name re-upload returns a renamed copy).
    Remote name is ASCII and prefixed with PROJ (4090 side stays English; easy per-project cleanup)."""
    remote = name or _ascii_name(path)
    if not remote.startswith(PROJ + "_"):
        remote = f"{PROJ}_{remote}"
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(path)[0] or "image/png"
    body = open(path, "rb").read()
    payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{remote}\"\r\n"
               f"Content-Type: {ctype}\r\n\r\n").encode() + body + f"\r\n--{boundary}--\r\n".encode()
    return _post("/upload/image", payload, {"Content-Type": f"multipart/form-data; boundary={boundary}"})["name"]


# ---------------------------------------------------------------- guards
def check_frames(length, approved_limit_s=None):
    """Production tiers 124/192. 243..345 only when the user approved and the project header limit
    (approved_limit_s) was written down >= length/24. No unvalidated tiers like 209/226."""
    if not isinstance(length, int) or (length - 5) % 17 != 0:
        raise ValueError(f"length={length} violates (F-5)%17==0 (use 124 or 192)")
    if length in DEFAULT_FRAMES:
        return length
    if length < 243 or length > MAX_FRAMES:
        raise ValueError(f"length={length} is not a production tier (124/192; 243..{MAX_FRAMES} need approval)")
    if approved_limit_s is None or length / 24 > approved_limit_s:
        raise ValueError(f"length={length} ({length/24:.2f}s) needs user approval + project limit >= {length/24:.2f}s")
    return length


MAX_AREA = 768 * 1344   # H3 open-weight pixel-area ceiling (1376x768 etc. are out of distribution)


def check_canvas(width, height, draft=False):
    """Workflow §3.3: short edge 768, both sides multiples of 32, area <= 768x1344.
    16:9 -> 1344x768, 9:16 -> 768x1344; other ratios (1:1 = 768x768, 4:3 = 1024x768 ...) are allowed.
    draft=True skips the check (layout tests only; drafts never go into the final cut)."""
    if draft:
        return
    if width % 32 or height % 32 or min(width, height) != 768 or width * height > MAX_AREA:
        raise ValueError(f"{width}x{height} is not an H3 native canvas (short edge 768, x32, area <= 768x1344)")


# ---------------------------------------------------------------- keyframes (Qwen-Image 2.1)
NEG = "文字，字母，logo，水印，多余手臂，畸形手指，噪点，颗粒，过曝，浓妆，欧美面孔，卡通，插画"
# Drop 噪点/颗粒/过曝 when the storyboard explicitly asks to keep or add that look:
NEG_NO_GRAIN = "文字，字母，logo，水印，多余手臂，畸形手指，浓妆，欧美面孔，卡通，插画"


def wf_qwen_t2i(prompt, tag, seed, width=768, height=1344, neg=NEG):
    """Text keyframe. TextEncodeQwenImage21: clip + negative_prompt + resolution(INT 1024) required; outputs [0]=pos [1]=neg."""
    return {
     "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_int8_convrot.safetensors", "weight_dtype": "default"}},
     "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
     "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
     "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "prompt": prompt, "negative_prompt": neg, "resolution": 1024}},
     "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
     "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "seed": seed, "steps": 20, "cfg": 3.5, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0, "latent_image": ["5", 0]}},
     "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
     "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": f"{PROJ}/{tag}", "format": "auto"}}}


def _add_deliv(wf, src_node, tag, del_w, del_h):
    wf["10"] = {"class_type": "ImageScale", "inputs": {"image": [src_node, 0], "upscale_method": "lanczos",
                                                       "width": del_w, "height": del_h, "crop": "disabled"}}
    wf["11"] = {"class_type": "SaveImage", "inputs": {"images": ["10", 0],
                                                      "filename_prefix": f"{PROJ}/{tag}_deliv", "format": "auto"}}
    return wf


def wf_qwen_t2i_kf2k(prompt, tag, seed, out_w=None, out_h=None, del_w=None, del_h=None, orient="portrait", neg=NEG):
    """Default keyframe builder: 4MP master (PROJ/tag) + Lanczos delivery copy (PROJ/tag_deliv) for FastH3.
    orient="portrait" -> 1536x2688 / 768x1344; "landscape" -> 2688x1536 / 1344x768.
    Explicit out_*/del_* still accepted (old call style). The _deliv copy is what goes into i2v."""
    mw, mh = MASTER[orient]
    dw, dh = CANVAS[orient]
    out_w, out_h = out_w or mw, out_h or mh
    del_w, del_h = del_w or dw, del_h or dh
    check_canvas(del_w, del_h)
    if (out_w, out_h) != (del_w * 2, del_h * 2):
        raise ValueError(f"master {out_w}x{out_h} must be 2x the delivery canvas {del_w}x{del_h}")
    wf = wf_qwen_t2i(prompt, tag, seed, width=out_w, height=out_h, neg=neg)
    return _add_deliv(wf, "7", tag, del_w, del_h)


def wf_qwen_edit(base_img, prompt, tag, seed, neg=NEG, deliver=None, *, source_kind="standard_master"):
    """EditPlus single-layer derive from a registered master (edit on the 4MP master).
    EditPlus has ONE conditioning output -> neg needs its own node. cfg 3.5.
    deliver=(w, h) also saves a Lanczos delivery copy PROJ/tag_deliv for i2v."""
    if source_kind != "standard_master":
        raise ValueError("Grid tiles cannot be used or relabelled as the normal 4MP edit master")
    wf = {
     "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_int8_convrot.safetensors", "weight_dtype": "default"}},
     "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
     "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
     "5": {"class_type": "LoadImage", "inputs": {"image": base_img}},
     "4": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["2", 0], "prompt": prompt, "vae": ["3", 0], "image1": ["5", 0]}},
     "4b": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["2", 0], "prompt": neg, "vae": ["3", 0], "image1": ["5", 0]}},
     "6": {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["3", 0]}},
     "9": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4b", 0], "seed": seed, "steps": 20, "cfg": 3.5, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0, "latent_image": ["6", 0]}},
     "7": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
     "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": f"{PROJ}/{tag}", "format": "auto"}}}
    if deliver:
        check_canvas(*deliver)
        _add_deliv(wf, "7", tag, *deliver)
    return wf


# ---------------------------------------------------------------- video (FastH3)

def wf_qwen_reference(reference_images, prompt, tag, seed, out_w, out_h, del_w, del_h, neg=NEG, *, project=None):
    """Opt-in identity-conditioned keyframe graph, not a visual-quality guarantee.

    Only existing Qwen EditPlus image1..3 slots are used. The references are
    conditioning inputs; an empty latent fixes the normal per-shot 4MP output
    canvas instead of inheriting a reference-sheet layout or size. Runtime
    capability and visual checks remain required before real production.
    """
    if (not isinstance(reference_images, (list, tuple)) or not 1 <= len(reference_images) <= 3
            or any(not isinstance(n, str) or not n.strip() or n.startswith('/') or ':' in n
                   or '\\' in n or any(c in n for c in ('\r', '\n', '\x00'))
                   or any(p in ('', '.', '..') for p in n.split('/')) for n in reference_images)
            or len(set(reference_images)) != len(reference_images)):
        raise ValueError('Provide one to three distinct actual uploaded reference image names')
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError('Reference seed must be an unsigned integer')
    if any(type(v) is not int or v < 1 for v in (out_w, out_h, del_w, del_h)):
        raise ValueError('Reference canvas dimensions must be positive integers')
    if (min(del_w, del_h) != 768 or del_w % 32 or del_h % 32 or del_w * del_h > 768 * 1344
            or (out_w, out_h) != (del_w * 2, del_h * 2)):
        raise ValueError('Reference keyframes retain the normal 4MP master and native delivery canvas')
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(neg, str):
        raise ValueError('Reference prompt must be nonempty and negative text must be a string')
    project = PROJ if project is None else project
    if (not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project)
            or not isinstance(tag, str) or not tag.strip() or tag.startswith('/') or '\\' in tag
            or ':' in tag or any(ord(c) < 32 for c in tag)
            or any(x in ('', '.', '..') for x in tag.split('/'))):
        raise ValueError('Safe explicit project and output tag required')
    wf = {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'qwen_image_2.1_int8_convrot.safetensors', 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': 'qwen3vl_8b_int8_convrot.safetensors', 'type': 'qwen_image', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'qwen_image_2.1_vae_bf16.safetensors'}},
        '4': {'class_type': 'TextEncodeQwenImageEditPlus', 'inputs': {'clip': ['2', 0], 'prompt': prompt, 'vae': ['3', 0]}},
        '4b': {'class_type': 'TextEncodeQwenImageEditPlus', 'inputs': {'clip': ['2', 0], 'prompt': neg, 'vae': ['3', 0]}},
        '6': {'class_type': 'EmptySD3LatentImage', 'inputs': {'width': out_w, 'height': out_h, 'batch_size': 1}},
        '9': {'class_type': 'KSampler', 'inputs': {'model': ['1', 0], 'positive': ['4', 0], 'negative': ['4b', 0],
               'seed': seed, 'steps': 20, 'cfg': 3.5, 'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1.0, 'latent_image': ['6', 0]}},
        '7': {'class_type': 'VAEDecode', 'inputs': {'samples': ['9', 0], 'vae': ['3', 0]}},
        '8': {'class_type': 'SaveImage', 'inputs': {'images': ['7', 0], 'filename_prefix': f'{project}/{tag}', 'format': 'auto'}},
        '10': {'class_type': 'ImageScale', 'inputs': {'image': ['7', 0], 'upscale_method': 'lanczos', 'width': del_w, 'height': del_h, 'crop': 'disabled'}},
        '11': {'class_type': 'SaveImage', 'inputs': {'images': ['10', 0], 'filename_prefix': f'{project}/{tag}_deliv', 'format': 'auto'}}}
    for number, name in enumerate(reference_images, 1):
        node = ('5', '5b', '5c')[number - 1]
        wf[node] = {'class_type': 'LoadImage', 'inputs': {'image': name}}
        for enc in ('4', '4b'):
            wf[enc]['inputs']['image' + str(number)] = [node, 0]
    return wf

def wf_h3_t2v(prompt, length, prefix, seed, width=1344, height=768):
    """T2V placeholder (2026-10-16 official route, NOT yet wired).
    Official basis: template video_fastvideo_fasth3_t2v.json (official H3 Preview validated T2VA only;
    i2v is the compat path). Wiring = open template on 4090 -> swap ONLY §2 local model names ->
    export API JSON -> replace this stub with the exported graph -> pass one 124-frame shot -> record
    in workflow §3.4 -> then usable. FORBIDDEN to hand-assemble by deleting i2v first_frame.
    Until wired: raises; use i2v_first instead."""
    raise RuntimeError(
        "T2V待验证：官方模板 video_fastvideo_fasth3_t2v.json 尚未接入导出。"
        "禁止手搭节点图（删first_frame臆装能跑）；改走 wf_fasth3_i2v 或先完成模板导入+124帧试跑。")


def wf_fasth3_i2v(kfs, prompt, length, prefix, seed, width=768, height=1344, approved_limit_s=None, draft=False,
                  keyframe_sizes=None, approved_max_seconds=None, approval_note=None):
    """FastH3 i2v, 1 or 2 keyframes (EXACT delivery-canvas px: first stretched, last center-crop).
    kfs = list of uploaded names ([first] or [first, last]) — a bare string would be sliced per character.
    kfs must be the Lanczos _deliv delivery copies (never full-size 4MP masters).
    length: 124=5.17s, 192=8s by default; 243..345 only with an approved limit (see check_frames).
    drama-showrunner (scripts/comfy_run.py) calls this with keyframe_sizes=[(w,h),...] and, over 10s,
    approved_max_seconds + approval_note — keep these parameters when changing the signature."""
    if isinstance(kfs, str) or not isinstance(kfs, (list, tuple)) or not 1 <= len(kfs) <= 2:
        raise TypeError("kfs must be a list: [first] or [first, last]")
    if approved_max_seconds is not None:
        if not (isinstance(approval_note, str) and approval_note.strip()):
            raise ValueError("approved_max_seconds needs a non-empty approval_note (the user's approval)")
        approved_limit_s = approved_max_seconds if approved_limit_s is None else min(approved_limit_s, approved_max_seconds)
    check_frames(length, approved_limit_s)
    check_canvas(width, height, draft)
    if keyframe_sizes is not None:
        sizes = [tuple(x) for x in keyframe_sizes]
        if len(sizes) != len(kfs) or any(sz != (width, height) for sz in sizes):
            raise ValueError(f"keyframe sizes {sizes} must all equal the canvas {(width, height)} (first frame is stretched, last is center-cropped)")
    n = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
     "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors", "type": "minimax", "device": "default"}},  # production default; old _awq deprecated
     "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
     "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
     "6": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "width": width, "height": height, "length": length, "first_frame": ["5", 0]}},
     "7": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["1", 0], "shift_video": 10.0, "shift_audio": 3.0}},
     "8": {"class_type": "BlockSparseAttention", "inputs": {"model": ["7", 0], "selection": "vsa", "selection.keep_percent": 10.0, "start_percent": 0.2, "end_percent": 1.0, "dense_blocks": "", "min_tokens": 12288, "extra_tokens": 256, "sink_conditioning": "exact_kv_and_rows", "verbose": False}},
     "9": {"class_type": "ModelAttentionBackend", "inputs": {"model": ["8", 0], "attention": "comfy kitchen attention"}},
     "10": {"class_type": "BasicGuider", "inputs": {"model": ["9", 0], "conditioning": ["6", 0]}},
     "11": {"class_type": "BasicScheduler", "inputs": {"model": ["9", 0], "scheduler": "simple", "steps": 8, "denoise": 1.0}},
     "12": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
     "13": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
     "14": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["13", 0], "guider": ["10", 0], "sampler": ["12", 0], "sigmas": ["11", 0], "latent_image": ["6", 1]}},
     "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["3", 0]}},
     "16": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["14", 1], "vae": ["4", 0]}},
     "17": {"class_type": "CreateVideo", "inputs": {"images": ["15", 0], "fps": 24.0, "audio": ["16", 0]}},
     "18": {"class_type": "SaveVideo", "inputs": {"video": ["17", 0], "filename_prefix": f"{PROJ}/{prefix}", "format": "auto"}},
     "5": {"class_type": "LoadImage", "inputs": {"image": kfs[0]}}}
    if len(kfs) > 1:
        n["5b"] = {"class_type": "LoadImage", "inputs": {"image": kfs[1]}}
        n["6"]["inputs"]["last_frame"] = ["5b", 0]
    return n


# ---------------------------------------------------------------- voice (CosyVoice on 4090)
COSY_VOICES = ("中文女", "中文男", "日语男", "粤语女", "英文女", "英文男", "韩语女")


def wf_cosyvoice(text, voice, prefix, seed, speed=0.95):
    """Pre-trained voice route (instruct_text did not work on the installed 30A3 build, 2026-10-21).
    Output lands in history outputs[*]['audio'] (list of dicts); wait_done() collects it.
    speed 0.8-1.45 usable; 0.88-0.95 for restrained / older-sounding delivery."""
    if voice not in COSY_VOICES:
        raise ValueError(f"voice must be one of {COSY_VOICES}")
    if not 0.8 <= speed <= 1.45:
        raise ValueError("speed must be within 0.8-1.45")
    return {
        "1": {"class_type": "CosyVoiceNode", "inputs": {"tts_text": text, "speed": float(speed), "inference_mode": "预训练音色", "sft_dropdown": voice, "seed": seed}},
        "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": f"{PROJ}/{prefix}"}},
    }
