#!/usr/bin/env python3
"""Validate and prepare the AI fabric lab example."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = REPO_ROOT / "examples" / "ai-fabric-lab"
SRC_ROOT = REPO_ROOT / "src"
REVISION_HEX_LEN = 40

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workerbee.manifests import validate_stage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_fabric_lab.py")
    parser.add_argument("--root", type=Path, default=EXAMPLE_ROOT)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    sub = parser.add_subparsers(dest="cmd", required=True)

    validate = sub.add_parser("validate", help="Validate the static lab bundle")
    validate.add_argument("--stage", type=Path, default=None)
    validate.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    init_storage = sub.add_parser("init-storage", help="Create the /srv/storage lab layout")
    init_storage.add_argument("--storage-root", type=Path, default=None)
    init_storage.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    track = sub.add_parser("print-track", help="Print one model track")
    track.add_argument("track")
    track.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    if args.cmd == "validate":
        result = validate_lab(root, stage=args.stage)
        return _emit(result, json_out=args.json)
    if args.cmd == "init-storage":
        result = init_storage_layout(root, storage_root=args.storage_root)
        return _emit(result, json_out=args.json)
    if args.cmd == "print-track":
        result = print_track(root, args.track)
        return _emit(result, json_out=args.json)
    return 2


def validate_lab(root: Path, *, stage: Path | None = None) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    model_tracks = _load_json(root / "model-tracks.json")
    storage_layout = _load_json(root / "storage-layout.json")
    findings.extend(_validate_model_tracks(model_tracks))
    findings.extend(_validate_storage_layout(storage_layout))
    stage_dir = (stage or root / "stage").expanduser().resolve()
    stage_validation = validate_stage(stage_dir)
    if not stage_validation.get("ok"):
        for finding in stage_validation.get("findings", []):
            if isinstance(finding, dict):
                findings.append(
                    {
                        "level": str(finding.get("level") or "error"),
                        "code": str(finding.get("code") or "STAGE_VALIDATION"),
                        "message": str(finding.get("message") or finding),
                    }
                )
    return {
        "ok": not [item for item in findings if item["level"] == "error"],
        "root": str(root),
        "stage_dir": str(stage_dir),
        "default_track": model_tracks.get("default_track"),
        "tracks": sorted((model_tracks.get("tracks") or {}).keys()),
        "stage": stage_validation,
        "findings": findings,
    }


def init_storage_layout(root: Path, *, storage_root: Path | None = None) -> dict[str, Any]:
    model_tracks = _load_json(root / "model-tracks.json")
    storage_layout = _load_json(root / "storage-layout.json")
    findings = _validate_model_tracks(model_tracks) + _validate_storage_layout(storage_layout)
    if [item for item in findings if item["level"] == "error"]:
        return {"ok": False, "findings": findings}
    target_root = storage_root or Path(str(storage_layout["root"]))
    target_root = target_root.expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    created = []
    for relative in storage_layout["directories"]:
        directory = target_root / str(relative)
        directory.mkdir(parents=True, exist_ok=True)
        created.append(str(directory))
    config_dir = target_root / "config"
    shutil.copy2(root / "model-tracks.json", config_dir / "model-tracks.json")
    shutil.copy2(root / "storage-layout.json", config_dir / "storage-layout.json")
    return {
        "ok": True,
        "root": str(target_root),
        "created": created,
        "config": [
            str(config_dir / "model-tracks.json"),
            str(config_dir / "storage-layout.json"),
        ],
        "findings": findings,
    }


def print_track(root: Path, track_name: str) -> dict[str, Any]:
    model_tracks = _load_json(root / "model-tracks.json")
    tracks = model_tracks.get("tracks") if isinstance(model_tracks.get("tracks"), dict) else {}
    track = tracks.get(track_name)
    if not isinstance(track, dict):
        return {
            "ok": False,
            "findings": [
                {
                    "level": "error",
                    "code": "UNKNOWN_TRACK",
                    "message": f"unknown model track: {track_name}",
                }
            ],
        }
    return {"ok": True, "track": track_name, "config": track}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return payload


def _validate_model_tracks(payload: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if payload.get("api_version") != "ai-fabric.model-tracks/v1":
        findings.append(
            _finding("error", "MODEL_TRACKS_API_VERSION", "unexpected model-tracks API")
        )
    tracks = payload.get("tracks")
    if not isinstance(tracks, dict):
        return findings + [_finding("error", "MODEL_TRACKS_MISSING", "tracks must be an object")]
    expected = set(payload.get("expected_tracks") or ["smoke", "baseline", "quality"])
    actual = set(tracks)
    missing = sorted(expected - actual)
    if missing:
        findings.append(_finding("error", "MODEL_TRACKS_MISSING_EXPECTED", ",".join(missing)))
    if payload.get("default_track") not in tracks:
        findings.append(_finding("error", "MODEL_TRACKS_DEFAULT", "default_track is not defined"))
    for track_name, track in tracks.items():
        if not isinstance(track, dict):
            findings.append(
                _finding("error", "MODEL_TRACK_INVALID", f"{track_name} is not an object")
            )
            continue
        for lane in ("coordinator", "expert"):
            lane_config = track.get(lane)
            if not isinstance(lane_config, dict):
                findings.append(_finding("error", "MODEL_LANE_MISSING", f"{track_name}.{lane}"))
                continue
            findings.extend(_validate_lane(track_name, lane, lane_config))
    return findings


def _validate_lane(track: str, lane: str, config: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for key in ("model", "revision", "port", "served_model_name", "max_model_len"):
        if config.get(key) in (None, ""):
            findings.append(_finding("error", "MODEL_LANE_REQUIRED", f"{track}.{lane}.{key}"))
    revision = str(config.get("revision") or "")
    revision_pinned = len(revision) == REVISION_HEX_LEN and all(
        char in "0123456789abcdef" for char in revision
    )
    if not revision_pinned:
        findings.append(_finding("error", "MODEL_REVISION_NOT_PINNED", f"{track}.{lane}"))
    utilization = float(config.get("gpu_memory_utilization") or 0)
    if utilization <= 0 or utilization >= 0.9:
        findings.append(_finding("error", "MODEL_GPU_UTILIZATION", f"{track}.{lane}"))
    port = int(config.get("port") or 0)
    if port < 1024 or port > 65535:
        findings.append(_finding("error", "MODEL_PORT", f"{track}.{lane}"))
    return findings


def _validate_storage_layout(payload: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if payload.get("api_version") != "ai-fabric.storage-layout/v1":
        findings.append(_finding("error", "STORAGE_LAYOUT_API_VERSION", "unexpected storage API"))
    root = Path(str(payload.get("root") or ""))
    if not root.is_absolute():
        findings.append(_finding("error", "STORAGE_ROOT_ABSOLUTE", "storage root must be absolute"))
    directories = payload.get("directories")
    if not isinstance(directories, list) or not directories:
        findings.append(
            _finding("error", "STORAGE_DIRECTORIES", "directories must be a non-empty list")
        )
        return findings
    required = {
        "config",
        "models/hf-cache",
        "adapters/expert",
        "corpus/k1s",
        "corpus/workerbee",
        "corpus/python",
        "corpus/hyperon",
        "qdrant",
        "mongo",
        "redis",
        "das",
        "runs",
        "artifacts/indexes",
    }
    actual = {str(item) for item in directories}
    missing = sorted(required - actual)
    if missing:
        findings.append(_finding("error", "STORAGE_DIRECTORIES_MISSING", ",".join(missing)))
    absolute_children = [item for item in actual if Path(item).is_absolute()]
    if absolute_children:
        findings.append(
            _finding("error", "STORAGE_CHILD_ABSOLUTE", ",".join(sorted(absolute_children)))
        )
    return findings


def _finding(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def _emit(result: dict[str, Any], *, json_out: bool) -> int:
    if json_out:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        status = "ok" if result.get("ok") else "failed"
        print(f"ai-fabric-lab {status}")
        for finding in result.get("findings", []):
            print(f"{finding['level']}: {finding['code']}: {finding['message']}")
        if result.get("root"):
            print(f"root: {result['root']}")
        if result.get("stage_dir"):
            print(f"stage: {result['stage_dir']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
