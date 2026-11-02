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

"""Offline provenance admission for experimental grid tiles; never crops/renders."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct


def asset(root, value):
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError("An actual project-relative path and SHA256 are required")
    name = value["path"]
    if (not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or ":" in name
            or "\x00" in name or any(p in {"", ".", ".."} for p in name.split("/"))):
        raise ValueError("Asset path must remain inside the project")
    root = Path(root).resolve()
    path = root
    for part in name.split("/"):
        path /= part
        if path.is_symlink():
            raise ValueError("Asset paths cannot traverse symlinks")
    expected = value["sha256"]
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Invalid asset SHA256")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("Missing or changed source asset: " + name)
    return path


def png_size(path):
    with Path(path).open("rb") as handle:
        header = handle.read(24)
    if (len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n"
            or header[8:16] != b"\x00\x00\x00\rIHDR"):
        raise ValueError("Grid provenance requires actual PNG IHDR metadata")
    size = list(struct.unpack(">II", header[16:24]))
    if min(size) < 1:
        raise ValueError("Invalid PNG dimensions")
    return size


def validate_grid(manifest, root, *, usage="video_input"):
    fields = {"schema_version", "source_kind", "grid", "tile", "grid_size", "tile_size", "canvas",
              "layout", "cell_index", "crop_xyxy", "seed", "experiment_decision"}
    if not isinstance(manifest, dict) or set(manifest) != fields or manifest["schema_version"] != "grid-keyframe/1":
        raise ValueError("Expected a complete grid-keyframe/1 provenance record")
    if manifest["source_kind"] != "grid_tile_experiment" or manifest["layout"] != [2, 2]:
        raise ValueError("Only explicit experimental 2x2 grid tiles are supported")
    if usage not in {"video_input", "edit_master"}:
        raise ValueError("Unknown grid asset usage")
    if usage == "edit_master":
        raise ValueError("A ~1MP grid tile is not the normal per-shot 4MP edit master")
    decision = manifest["experiment_decision"]
    if (not isinstance(decision, str) or not decision.strip() or len(decision) > 1000):
        raise ValueError("Record the existing decision selecting the grid experiment")
    if type(manifest["seed"]) is not int or not 0 <= manifest["seed"] < 2 ** 64:
        raise ValueError("A reproducible grid seed is required")
    for key in ("grid_size", "tile_size", "canvas"):
        value = manifest[key]
        if not isinstance(value, list) or len(value) != 2 or any(type(x) is not int or x < 1 for x in value):
            raise ValueError("Positive integer dimensions required: " + key)
    if manifest["canvas"] not in ([1344, 768], [768, 1344]):
        raise ValueError("Grid experiment supports the existing native portrait/landscape canvases only")
    if manifest["grid_size"] != [n * 2 for n in manifest["canvas"]]:
        raise ValueError("The whole grid must be twice the canvas on each axis")
    if manifest["tile_size"] != manifest["canvas"]:
        raise ValueError("A tile is the native video input, not a 4MP master or an implicit upscale")
    index, box = manifest["cell_index"], manifest["crop_xyxy"]
    if type(index) is not int or not 0 <= index < 4:
        raise ValueError("cell_index must be within 0..3")
    if not isinstance(box, list) or len(box) != 4 or any(type(x) is not int for x in box):
        raise ValueError("crop_xyxy requires four integer half-open coordinates")
    w, h = manifest["canvas"]
    x, y = index % 2 * w, index // 2 * h
    if box != [x, y, x + w, y + h]:
        raise ValueError("Crop coordinates do not bind the declared exact grid cell")
    grid = asset(root, manifest["grid"])
    tile = asset(root, manifest["tile"])
    if grid == tile:
        raise ValueError("The full grid cannot be passed as its own production tile")
    if png_size(grid) != manifest["grid_size"] or png_size(tile) != manifest["tile_size"]:
        raise ValueError("Actual PNG dimensions differ from the grid contract")
    return {"source_kind": "grid_tile_experiment", "usable_as": "video_input_only",
            "grid_sha256": manifest["grid"]["sha256"], "tile_sha256": manifest["tile"]["sha256"],
            "crop_xyxy": list(box), "canvas": list(manifest["canvas"]),
            "per_shot_pixels": w * h, "standard_master_pixels": w * h * 4,
            "quality": "UNVERIFIED", "pixel_crop_check": "NOT_PERFORMED",
            "check_scope": "File hashes, declared crop geometry and PNG headers only; no visual acceptance."}


def validate_job_sources(job, root, input_names):
    """Bind explicitly declared grid sources to the exact first/last input files."""
    sources = job.get("keyframe_sources")
    if sources is None:
        return []  # Existing non-grid jobs retain their former input contract.
    if (job.get("kind") != "vid" or not isinstance(sources, list)
            or len(sources) != len(input_names)):
        raise ValueError("keyframe_sources must cover this video job's actual first/last inputs")
    verified = []
    for source, name in zip(sources, input_names):
        if source is None:
            verified.append(None)
            continue
        result = validate_grid(source, root, usage="video_input")
        actual = Path(root).resolve() / name
        if asset(root, source["tile"]) != actual.resolve():
            raise ValueError("Grid tile is not the actual uploaded first/last input")
        verified.append(result)
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--root", required=True)
    parser.add_argument("--usage", choices=("video_input", "edit_master"), default="video_input")
    args = parser.parse_args()
    try:
        value = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        print(json.dumps(validate_grid(value, args.root, usage=args.usage), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc), "quality": "UNVERIFIED"}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
