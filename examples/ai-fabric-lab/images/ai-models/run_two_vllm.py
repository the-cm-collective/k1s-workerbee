#!/usr/bin/env python3
"""Start one or both vLLM lanes for the AI fabric lab."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.getenv("AI_FABRIC_CONFIG", "/etc/ai-fabric/model-tracks.json"))
LANES = ("coordinator", "expert")


def main() -> int:
    config = _load_config(CONFIG_PATH)
    track_name = os.getenv("AI_FABRIC_TRACK") or str(config.get("default_track") or "baseline")
    tracks = config.get("tracks") if isinstance(config.get("tracks"), dict) else {}
    track = tracks.get(track_name)
    if not isinstance(track, dict):
        print(f"unknown AI_FABRIC_TRACK={track_name!r}", file=sys.stderr, flush=True)
        return 2

    defaults = config.get("run_defaults") if isinstance(config.get("run_defaults"), dict) else {}
    os.environ.setdefault(
        "VLLM_WORKER_MULTIPROC_METHOD",
        str(defaults.get("vllm_worker_multiproc_method") or "spawn"),
    )
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", str(defaults.get("cuda_visible_devices") or "0"))
    download_dir = str(defaults.get("download_dir") or "/models/hf-cache")
    stagger = int(
        os.getenv(
            "AI_FABRIC_STARTUP_STAGGER_SECONDS",
            defaults.get("startup_stagger_seconds", 20),
        )
    )

    processes: list[subprocess.Popen[bytes]] = []
    stopping = False

    def stop_children(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)

    requested_lane = os.getenv("AI_FABRIC_LANE")
    if requested_lane and requested_lane not in LANES:
        print(f"unknown AI_FABRIC_LANE={requested_lane!r}", file=sys.stderr, flush=True)
        return 2
    lanes = (requested_lane,) if requested_lane else LANES

    for lane in lanes:
        lane_config = track.get(lane)
        if not isinstance(lane_config, dict):
            print(f"track {track_name!r} is missing lane {lane!r}", file=sys.stderr, flush=True)
            return 2
        command = _vllm_command(lane_config, defaults=defaults, download_dir=download_dir)
        print(f"starting {lane}: {' '.join(command)}", flush=True)
        processes.append(subprocess.Popen(command))
        if lane == "coordinator" and len(lanes) > 1:
            time.sleep(stagger)

    while not stopping:
        for process in processes:
            code = process.poll()
            if code is not None:
                for other in processes:
                    if other is not process and other.poll() is None:
                        other.terminate()
                return int(code)
        time.sleep(2)
    return 0


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return payload


def _vllm_command(
    lane: dict[str, Any], *, defaults: dict[str, Any], download_dir: str
) -> list[str]:
    lora_modules = _lora_module_args(lane.get("lora_modules"))
    command = [
        "python3",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--host",
        "0.0.0.0",  # noqa: S104 - container service listens on the pod network.
        "--port",
        str(lane["port"]),
        "--model",
        str(lane["model"]),
        "--revision",
        str(lane["revision"]),
        "--served-model-name",
        str(lane["served_model_name"]),
        "--download-dir",
        download_dir,
        "--max-model-len",
        str(lane["max_model_len"]),
        "--gpu-memory-utilization",
        str(lane["gpu_memory_utilization"]),
    ]
    quantization = lane.get("quantization")
    if quantization:
        command.extend(["--quantization", str(quantization)])
    attention_backend = lane.get("attention_backend") or defaults.get("attention_backend")
    if attention_backend:
        command.extend(["--attention-backend", str(attention_backend)])
    if lane.get("enable_lora") or lora_modules:
        command.append("--enable-lora")
    if lora_modules:
        command.append("--lora-modules")
        command.extend(lora_modules)
    if lane.get("max_loras") is not None:
        command.extend(["--max-loras", str(lane["max_loras"])])
    if lane.get("max_lora_rank") is not None:
        command.extend(["--max-lora-rank", str(lane["max_lora_rank"])])
    return command


def _lora_module_args(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("lora_modules must be a list")
    modules: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item.strip():
                modules.append(item.strip())
            continue
        if not isinstance(item, dict):
            raise TypeError("lora_modules entries must be strings or objects")
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        if not name or not path:
            raise ValueError("lora_modules object entries require name and path")
        modules.append(f"{name}={path}")
    return modules


if __name__ == "__main__":
    raise SystemExit(main())
