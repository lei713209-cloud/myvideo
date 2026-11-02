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

"""Offline dialogue planning/evidence checks. No speech model, ASR or media tools."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

STATES = {"PASS", "FAIL", "NA", "UNVERIFIED"}
ENGINES = {"cosyvoice_node_pretrained", "cosyvoice3_instruct2"}
PREFIX = "You are a helpful assistant. "
END = "<|endofprompt|>"


def asset(root, value):
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError("An actual project-relative audio path and SHA256 are required")
    name = value["path"]
    if (not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or ":" in name
            or "\x00" in name or any(p in {"", ".", ".."} for p in name.split("/"))):
        raise ValueError("Audio path must remain inside the project")
    path = Path(root).resolve()
    for part in name.split("/"):
        path /= part
        if path.is_symlink():
            raise ValueError("Audio paths cannot traverse symlinks")
    expected = value["sha256"]
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Invalid audio SHA256")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("Missing or changed audio evidence: " + name)
    return path


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(value):
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Performance semantics must be finite JSON data") from exc
    return text_hash(data)


def performance_hash(line, engine):
    """Freeze every non-evidence line field, including reference and engine settings.

    Audio/review records are excluded to avoid circular hashes. Unrecognized planning
    fields are conservatively included, so adding a synthesis option cannot silently
    reuse evidence created under the previous request.
    """
    if not isinstance(line, dict) or engine not in ENGINES:
        raise ValueError("Choose the exact existing speech engine and line")
    evidence = {"audio", "audio_provenance", "duration_evidence", "listening", "asr",
                "asr_review", "transcript", "performance_sha256"}
    return _digest({"engine": engine, "line": {k: v for k, v in line.items() if k not in evidence}})


def exchange_hash(plan):
    """Bind the dry review to current ordered requests, actual audio and spacing."""
    if not isinstance(plan, dict) or not isinstance(plan.get("lines"), list):
        raise ValueError("A dialogue plan is required for dry-exchange provenance")
    return _digest({"engine": plan.get("engine"), "exchange_plan": plan.get("exchange_plan"),
                    "lines": [{"performance_sha256": performance_hash(line, plan.get("engine")),
                               "audio_sha256": (line.get("audio") or {}).get("sha256"),
                               "duration_evidence": line.get("duration_evidence"),
                               "pause_after_s": line.get("pause_after_s")} for line in plan["lines"]]})


def _text(value, name, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Nonempty bounded text required: " + name)
    return value


def _number(value, name, low=0, high=600):
    if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Invalid numeric field: " + name)
    return value


def cv3_instruction(direction):
    """Canonical CosyVoice3 input, isolated from the actual spoken sentence."""
    direction = _text(direction, "delivery", 1000).strip()
    if direction.startswith(PREFIX):
        direction = direction[len(PREFIX):]
    if direction.endswith(END):
        direction = direction[:-len(END)].rstrip()
    if not direction or "<|" in direction or "|>" in direction or PREFIX in direction:
        raise ValueError("Delivery may not contain duplicate prefixes or control tokens")
    return PREFIX + direction + END


def prepare_line(line, engine):
    if engine not in ENGINES or not isinstance(line, dict):
        raise ValueError("Choose the exact existing speech engine")
    for key in ("line_id", "speaker", "text", "context", "intention", "delivery"):
        _text(line.get(key), key)
    if line.get("lip") not in {"visible", "indistinct", "offscreen"}:
        raise ValueError("Dialogue needs its actual lip visibility")
    canonical = line["text"]
    expected = line.get("canonical_sha256")
    if expected != text_hash(canonical):
        raise ValueError("Canonical dialogue differs from its frozen SHA256")
    if "<|" in canonical or "|>" in canonical:
        raise ValueError("Speech text must not contain model control tokens")
    spoken = line.get("spoken_text", canonical)
    _text(spoken, "spoken_text")
    lexicon = line.get("pronunciation_lexicon", [])
    if not isinstance(lexicon, list):
        raise ValueError("Pronunciation lexicon must be a list")
    readings = {}
    for entry in lexicon:
        if not isinstance(entry, dict) or set(entry) != {"surface", "reading"}:
            raise ValueError("Pronunciation entries keep the original surface and a separate reading")
        if _text(entry["surface"], "surface") not in canonical:
            raise ValueError("Pronunciation surface is absent from the original dialogue")
        _text(entry["reading"], "reading", 200)
        if entry["surface"] in readings:
            raise ValueError("Duplicate pronunciation surface")
        readings[entry["surface"]] = entry["reading"]
    if spoken != canonical and (not lexicon or not isinstance(line.get("pronunciation_note"), str)
                                or not line["pronunciation_note"].strip()):
        raise ValueError("A changed TTS rendering requires a separate pronunciation lexicon and note")
    if spoken != canonical:
        pattern = "|".join(re.escape(x) for x in sorted(readings, key=len, reverse=True))
        rendered = re.sub(pattern, lambda m: readings[m.group()], canonical)
        if spoken != rendered:
            raise ValueError("Spoken rendering may change only declared pronunciation surfaces, never rewrite dialogue")
    if "<|" in spoken or "|>" in spoken:
        raise ValueError("Spoken rendering must not contain control tokens")
    _number(line.get("pause_after_s"), "pause_after_s", 0, 10)
    performance_sha = performance_hash(line, engine)
    if line.get("performance_sha256", performance_sha) != performance_sha:
        raise ValueError("Performance differs from its frozen semantic SHA256")
    return {"line_id": line["line_id"], "speaker": line["speaker"], "canonical_text": canonical,
            "canonical_sha256": expected, "spoken_text": spoken,
            "performance_sha256": performance_sha,
            "pronunciation_lexicon": lexicon,
            "instruction": cv3_instruction(line["delivery"]) if engine == "cosyvoice3_instruct2" else None,
            "engine": engine, "delivery_control": "instruction" if engine == "cosyvoice3_instruct2" else "not_supported_by_preset_node",
            "quality": "UNVERIFIED"}


def _listening(review, audio_sha, *, allow_na=False, binding_key=None, binding_hash=None):
    if not isinstance(review, dict) or review.get("status") not in STATES:
        raise ValueError("Listening review must explicitly use PASS/FAIL/NA/UNVERIFIED")
    if review["status"] == "NA" and not allow_na:
        raise ValueError("Dialogue listening is applicable and cannot be marked NA")
    if review["status"] in {"PASS", "FAIL"}:
        if (review.get("method") != "listening" or review.get("audio_sha256") != audio_sha
                or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()
                or not isinstance(review.get("evidence"), str) or not review["evidence"].strip()):
            raise ValueError("Actual listening conclusions must bind the exact audio, reviewer and observation")
        if binding_key is not None and review.get(binding_key) != binding_hash:
            raise ValueError("Listening evidence is stale or missing its " + binding_key)
    return review["status"]


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum())


def assess_asr(canonical_text, transcript, audio, root, *, listening=None, attempts=0, max_attempts=2):
    """Transcript differences are uncertainty; only listening can confirm a defect."""
    canonical_text = _text(canonical_text, "canonical_text")
    if not isinstance(transcript, str):
        raise ValueError("An existing transcript is required; this helper never runs ASR")
    asset(root, audio)
    if type(attempts) is not int or type(max_attempts) is not int or not 0 <= attempts <= max_attempts <= 5:
        raise ValueError("Persistent, bounded per-line attempts are required")
    state = "TEXT_MATCH_LISTENING_REQUIRED" if normalized(canonical_text) == normalized(transcript) else "ASR_UNCERTAIN"
    retry = False
    if listening is not None:
        status = _listening(listening, audio["sha256"], binding_key="canonical_sha256",
                            binding_hash=text_hash(canonical_text))
        if status == "PASS":
            state = "VERIFIED"
        elif status == "FAIL":
            if listening.get("issue") == "mispronunciation":
                state = "CONFIRMED_MISPRONUNCIATION"
                retry = attempts < max_attempts
            else:
                state = "CONFIRMED_OTHER_AUDIO_DEFECT"
    return {"status": state, "canonical_text": canonical_text, "canonical_sha256": text_hash(canonical_text),
            "audio_sha256": audio["sha256"], "transcript": transcript, "retry_eligible": retry,
            "auto_retry": False, "attempts": attempts, "max_attempts": max_attempts,
            "requires": "Explicit bounded retry decision after actual listening, or retain for listening.",
            "naturalness_from_asr": "UNVERIFIED"}


def replacement_policy(lip, *, picture_accepted, audio_defect_confirmed):
    if lip not in {"visible", "indistinct", "offscreen"} or type(picture_accepted) is not bool or type(audio_defect_confirmed) is not bool:
        raise ValueError("Actual lip class and explicit picture/audio evidence required")
    if lip == "visible":
        return {"audio_only_replacement": False, "next": "Rework the affected speaking shot or explicitly authorized audio-driven lip route.",
                "lipsync": "UNVERIFIED"}
    return {"audio_only_replacement": picture_accepted and audio_defect_confirmed,
            "next": "Replace only the confirmed failed line; preserve accepted picture and review the final mix.",
            "lipsync": "NA"}


def validate_plan(plan, root, *, require_audio=False):
    if not isinstance(plan, dict) or plan.get("schema_version") != "dialogue-performance/1":
        raise ValueError("Expected dialogue-performance/1")
    _text(plan.get("exchange_plan"), "exchange_plan", 4000)
    engine, lines = plan.get("engine"), plan.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 200:
        raise ValueError("A bounded dialogue line list is required")
    ids, prepared, pending = set(), [], []
    for line in lines:
        item = prepare_line(line, engine)
        if line.get("voice_reference") is not None:
            asset(root, line["voice_reference"])
        if item["line_id"] in ids:
            raise ValueError("Duplicate dialogue line ID")
        ids.add(item["line_id"])
        prepared.append(item)
        audio = line.get("audio")
        if audio is None:
            if require_audio:
                raise ValueError("Actual audio required: " + item["line_id"])
            pending.append(item["line_id"] + ":audio_not_generated")
            continue
        asset(root, audio)
        provenance = line.get("audio_provenance")
        if (not isinstance(provenance, dict) or provenance.get("audio_sha256") != audio["sha256"]
                or provenance.get("performance_sha256") != item["performance_sha256"]):
            raise ValueError("Audio provenance is missing or stale for the current performance_sha256")
        duration = line.get("duration_evidence")
        if (not isinstance(duration, dict) or duration.get("audio_sha256") != audio["sha256"]
                or duration.get("performance_sha256") != item["performance_sha256"]
                or duration.get("method") not in {"ffprobe", "wave_sample_count"}):
            raise ValueError("Measured duration must bind the exact audio and performance_sha256")
        _number(duration.get("seconds"), "measured seconds", .001, 600)
        item["duration_s"] = duration["seconds"]
        status = _listening(line.get("listening", {"status": "UNVERIFIED"}), audio["sha256"],
                            binding_key="performance_sha256", binding_hash=item["performance_sha256"])
        if status != "PASS":
            pending.append(item["line_id"] + ":listening_" + status)
    mixed = plan.get("dry_mix")
    exchange_sha = exchange_hash(plan)
    if mixed:
        asset(root, mixed)
        provenance = plan.get("dry_mix_provenance")
        if (not isinstance(provenance, dict) or provenance.get("audio_sha256") != mixed["sha256"]
                or provenance.get("exchange_sha256") != exchange_sha):
            raise ValueError("Dry mix provenance is missing or stale for the current exchange_sha256")
        mix_status = _listening(plan.get("dry_listening", {"status": "UNVERIFIED"}), mixed["sha256"],
                                binding_key="exchange_sha256", binding_hash=exchange_sha)
    else:
        mix_status = "UNVERIFIED"
    if mix_status != "PASS":
        pending.append("whole_exchange:dry_listening_" + mix_status)
    # This is recorded listening evidence, never an automatic artistic judgment.
    return {"schema_version": "dialogue-preflight/1", "prepared_lines": prepared, "exchange_sha256": exchange_sha,
            "ready_for_picture_timing": not pending, "pending": pending,
            "automatic_creative_qc": "UNVERIFIED", "downloads": "NOT_PERMITTED_BY_THIS_HELPER"}


# Stable application-facing names for the single-shot and drama adapters.
validate_audio_plan = validate_plan
validate_line = prepare_line
compile_line = prepare_line


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--root", required=True)
    parser.add_argument("--require-audio", action="store_true")
    args = parser.parse_args()
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        result = validate_plan(plan, args.root, require_audio=args.require_audio)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready_for_picture_timing"] else 10
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc), "automatic_creative_qc": "UNVERIFIED"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
