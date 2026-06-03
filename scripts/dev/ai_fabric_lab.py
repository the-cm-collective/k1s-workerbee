#!/usr/bin/env python3
"""Validate and prepare the AI fabric lab example."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = REPO_ROOT / "examples" / "ai-fabric-lab"
SRC_ROOT = REPO_ROOT / "src"
REVISION_HEX_LEN = 40
RUNTIME_OUTPUT_FILES = (
    "summary.json",
    "acceptance.json",
    "ai-runtime-profile.json",
    "operator-report.json",
    "requests.jsonl",
    "gpu-samples.jsonl",
    "health.json",
    "lane-readiness.json",
    "f5-evidence.json",
    "workerbee-status.json",
    "advisor-scenarios.json",
)
RUNTIME_SUITE_CHOICES = (
    "all",
    "acceptance-closeout",
    "mixed-soak",
    "quality-contract",
    "lora-plumbing",
    "lora-adapter-smoke",
    "evidence-closeout",
    "adapter-preflight",
    "quality-comparison",
    "stress-burst",
    "recovery-smoke",
    "advisor-scenarios",
)
ACCEPTANCE_RUN_API_VERSION = "workerbee.ai-fabric.acceptance-run/v1"
OPERATOR_REPORT_API_VERSION = "workerbee.ai-fabric.operator-report/v1"
AI_RUNTIME_PROFILE_API_VERSION = "k1s.fabric.ai-runtime-profile/v1"
AI_RUNTIME_PROFILE_KIND = "AIFabricRuntimeProfile"
SOAK_PROMOTION_DURATION_SECONDS = 1800
ACCEPTANCE_CLOSEOUT_SUITES = (
    "adapter-preflight",
    "lora-adapter-smoke",
    "quality-comparison",
    "stress-burst",
    "recovery-smoke",
    "advisor-scenarios",
    "evidence-closeout",
)
RUNTIME_ENDPOINT_SUITES = {
    "mixed-soak",
    "quality-contract",
    "lora-plumbing",
    "lora-adapter-smoke",
    "evidence-closeout",
    "quality-comparison",
    "stress-burst",
    "recovery-smoke",
    "advisor-scenarios",
}
RUNTIME_MODEL_SUITES = {
    "mixed-soak",
    "quality-contract",
    "lora-plumbing",
    "lora-adapter-smoke",
    "quality-comparison",
    "stress-burst",
    "recovery-smoke",
}
RUNTIME_PROMPT_SUITES = {
    "mixed-soak",
    "quality-contract",
    "quality-comparison",
    "lora-plumbing",
    "lora-adapter-smoke",
    "stress-burst",
    "recovery-smoke",
}
ADAPTER_VALIDATION_RELATIVE_PATH = "adapters/expert/validation"
ADAPTER_VALIDATION_MODEL_NAME = "k1s-code-expert-lora-smoke"
ADAPTER_EXPECTED_BASE_MODELS = (
    "Qwen/Qwen2.5-Coder-7B-Instruct",
    "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ",
)
ADAPTER_MAX_LORA_RANK = 16
RUNTIME_FACT_SOURCE = "workerbee.ai-fabric.runtime-facts/v1"
ADVISORY_DECISION_API_VERSION = "workerbee.ai-fabric.advisory-decision/v1"
ADVISOR_SCENARIO_EVAL_API_VERSION = "workerbee.ai-fabric.advisor-scenario-eval/v1"
RUNTIME_RELATIONSHIP_PREDICATES = (
    "owns_service",
    "depends_on",
    "serves_model",
    "requires_resource",
    "produced_artifact",
    "supports_advisory",
)
AI_FABRIC_SERVICE_DEPENDENCIES = {
    "ai-router": (
        "ai-coordinator",
        "ai-expert",
        "retrieval-indexer",
        "das-bridge",
        "qdrant",
    ),
    "retrieval-indexer": ("qdrant",),
    "das-bridge": ("mongo",),
}
AI_FABRIC_SERVICE_ADVISORY_SUPPORT = {
    "ai-router": ("advisory_trace", "lane_routing"),
    "retrieval-indexer": ("retrieval_evidence", "corpus_search"),
    "das-bridge": ("symbolic_evidence", "f5_query_evidence"),
    "ai-coordinator": ("coordinator_advisory_lane",),
    "ai-expert": ("expert_code_advisory_lane",),
}
AI_FABRIC_SERVICE_STORAGE_RESOURCES = {
    "ai-coordinator": ("models/hf-cache", "adapters/expert", "runs"),
    "ai-expert": ("models/hf-cache", "adapters/expert", "runs"),
    "retrieval-indexer": ("corpus", "artifacts/indexes"),
    "das-bridge": ("das",),
    "qdrant": ("qdrant",),
    "mongo": ("mongo",),
    "redis": ("redis",),
}

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workerbee.manifests import _load_yaml_documents, validate_stage  # noqa: E402

CORPUS_SUFFIXES = {".md", ".py", ".txt", ".yaml", ".yml", ".json", ".toml"}
CORPUS_IGNORE_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}

RUNTIME_URL_FALLBACKS = {
    "router": {
        "service_name": "ai-router.ai-fabric-lab.svc.cluster.local",
        "container_port": 8080,
    },
    "das": {
        "service_name": "das-bridge.ai-fabric-lab.svc.cluster.local",
        "container_port": 8081,
    },
    "retrieval": {
        "service_name": "retrieval-indexer.ai-fabric-lab.svc.cluster.local",
        "container_port": 8082,
    },
}
CORPUS_SOURCE_PATHS = {
    "workerbee": [
        "AGENTS.md",
        "README.md",
        "docs",
        "examples/ai-fabric-lab",
        "pyproject.toml",
        "scripts/dev/ai_fabric_lab.py",
        "src",
        "tests/test_ai_fabric_lab.py",
    ],
    "k1s": [
        "AGENTS.md",
        "README.md",
        "docs",
        "examples",
        "pyproject.toml",
        "src",
    ],
}


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

    sync_corpus_parser = sub.add_parser(
        "sync-corpus",
        help="Copy selected WorkerBee and k1s docs/source snapshots into /srv/storage",
    )
    sync_corpus_parser.add_argument("--storage-root", type=Path, default=None)
    sync_corpus_parser.add_argument("--workerbee-root", type=Path, default=REPO_ROOT)
    sync_corpus_parser.add_argument("--k1s-root", type=Path, default=REPO_ROOT.parent / "k1s")
    sync_corpus_parser.add_argument("--max-files", type=int, default=1500)
    sync_corpus_parser.add_argument("--reset", dest="reset", action="store_true", default=True)
    sync_corpus_parser.add_argument("--no-reset", dest="reset", action="store_false")
    sync_corpus_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    track = sub.add_parser("print-track", help="Print one model track")
    track.add_argument("track")
    track.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    runtime_facts = sub.add_parser("import-runtime-facts", help="Import lab facts into DAS")
    runtime_facts.add_argument("--stage", type=Path, default=None)
    runtime_facts.add_argument("--das-url", default="http://127.0.0.1:8081")
    runtime_facts.add_argument("--router-url", default="http://127.0.0.1:18180")
    runtime_facts.add_argument("--retrieval-url", default="http://127.0.0.1:18182")
    runtime_facts.add_argument("--project", default="")
    runtime_facts.add_argument("--track", default="")
    runtime_facts.add_argument("--k1s-root", type=Path, default=REPO_ROOT.parent / "k1s")
    runtime_facts.add_argument("--workerbee-status", type=Path, default=None)
    runtime_facts.add_argument(
        "--phase-report",
        type=Path,
        default=None,
        help="Optional k1s fabric phase assurance JSON report to import with runtime facts.",
    )
    runtime_facts.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    phase_facts = sub.add_parser("import-phase-facts", help="Import k1s F-phase facts into DAS")
    phase_facts.add_argument("--phase-report", type=Path, required=True)
    phase_facts.add_argument("--das-url", default="http://127.0.0.1:8081")
    phase_facts.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    f5_evidence = sub.add_parser("emit-f5-evidence", help="Emit WorkerBee F5 lab evidence")
    f5_evidence.add_argument("--storage-root", type=Path, default=None)
    f5_evidence.add_argument("--site-id", default="site-a")
    f5_evidence.add_argument("--peer-site-id", default="site-b")
    f5_evidence.add_argument("--project", default="")
    f5_evidence.add_argument("--track", default="")
    f5_evidence.add_argument("--query-id", default="ai-fabric-local-first-smoke")
    f5_evidence.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    runtime_validate = sub.add_parser(
        "validate-runtime",
        help="Run AI fabric runtime validation suites against a deployed WorkerBee lab",
    )
    runtime_validate.add_argument(
        "--suite",
        choices=RUNTIME_SUITE_CHOICES,
        default="all",
    )
    runtime_validate.add_argument("--prompts", type=Path, default=None)
    runtime_validate.add_argument("--storage-root", type=Path, default=None)
    runtime_validate.add_argument("--run-id", default="")
    runtime_validate.add_argument("--track", default="")
    runtime_validate.add_argument("--router-url", default="http://127.0.0.1:18180")
    runtime_validate.add_argument("--das-url", default="http://127.0.0.1:18181")
    runtime_validate.add_argument("--retrieval-url", default="http://127.0.0.1:18182")
    runtime_validate.add_argument("--duration-seconds", type=int, default=None)
    runtime_validate.add_argument("--workers", type=int, default=None)
    runtime_validate.add_argument("--worker-sleep-seconds", type=float, default=2.0)
    runtime_validate.add_argument("--gpu-sample-seconds", type=int, default=None)
    runtime_validate.add_argument("--request-timeout", type=int, default=300)
    runtime_validate.add_argument("--success-threshold", type=float, default=0.95)
    runtime_validate.add_argument("--vram-growth-mib-max", type=int, default=4096)
    runtime_validate.add_argument("--workerbee-status", type=Path, default=None)
    runtime_validate.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    if args.cmd == "validate":
        result = validate_lab(root, stage=args.stage)
        return _emit(result, json_out=args.json)
    if args.cmd == "init-storage":
        result = init_storage_layout(root, storage_root=args.storage_root)
        return _emit(result, json_out=args.json)
    if args.cmd == "sync-corpus":
        result = sync_corpus(
            root,
            storage_root=args.storage_root,
            workerbee_root=args.workerbee_root,
            k1s_root=args.k1s_root,
            max_files=args.max_files,
            reset=args.reset,
        )
        return _emit(result, json_out=args.json)
    if args.cmd == "print-track":
        result = print_track(root, args.track)
        return _emit(result, json_out=args.json)
    if args.cmd == "import-runtime-facts":
        result = import_runtime_facts(
            root,
            stage=args.stage,
            das_url=args.das_url,
            router_url=args.router_url,
            retrieval_url=args.retrieval_url,
            project=args.project,
            track=args.track or None,
            k1s_root=args.k1s_root,
            phase_report=args.phase_report,
            workerbee_status=args.workerbee_status,
        )
        return _emit(result, json_out=args.json)
    if args.cmd == "import-phase-facts":
        result = import_phase_facts(
            phase_report=args.phase_report,
            das_url=args.das_url,
        )
        return _emit(result, json_out=args.json)
    if args.cmd == "emit-f5-evidence":
        result = emit_f5_evidence(
            root,
            storage_root=args.storage_root,
            site_id=args.site_id,
            peer_site_id=args.peer_site_id,
            project=args.project,
            track=args.track or None,
            query_id=args.query_id,
        )
        return _emit(result, json_out=args.json)
    if args.cmd == "validate-runtime":
        result = validate_runtime(
            root,
            suite=args.suite,
            prompts=args.prompts,
            storage_root=args.storage_root,
            run_id=args.run_id or None,
            track=args.track or None,
            router_url=args.router_url,
            das_url=args.das_url,
            retrieval_url=args.retrieval_url,
            duration_seconds=args.duration_seconds,
            workers=args.workers,
            worker_sleep_seconds=args.worker_sleep_seconds,
            gpu_sample_seconds=args.gpu_sample_seconds,
            request_timeout=args.request_timeout,
            success_threshold=args.success_threshold,
            vram_growth_mib_max=args.vram_growth_mib_max,
            workerbee_status=args.workerbee_status,
        )
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


def sync_corpus(
    root: Path,
    *,
    storage_root: Path | None = None,
    workerbee_root: Path = REPO_ROOT,
    k1s_root: Path = REPO_ROOT.parent / "k1s",
    max_files: int = 1500,
    reset: bool = True,
) -> dict[str, Any]:
    init_result = init_storage_layout(root, storage_root=storage_root)
    if not init_result.get("ok"):
        return init_result
    target_root = Path(str(init_result["root"]))
    corpus_root = target_root / "corpus"
    copied: dict[str, int] = {}
    skipped_sources: list[str] = []
    findings: list[dict[str, str]] = []
    source_roots = {
        "workerbee": workerbee_root.expanduser().resolve(),
        "k1s": k1s_root.expanduser().resolve(),
    }
    total = 0
    for source_name, source_root in source_roots.items():
        target_dir = corpus_root / source_name
        if reset and target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        if not source_root.exists():
            skipped_sources.append(f"{source_name}:{source_root}")
            copied[source_name] = 0
            continue
        source_count = 0
        for path in _iter_corpus_files(source_root, source_name):
            if source_count >= max_files:
                findings.append(
                    _finding(
                        "warning",
                        "CORPUS_MAX_FILES",
                        f"stopped {source_name} after copying {max_files} files",
                    )
                )
                break
            relative = path.relative_to(source_root)
            destination = target_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            total += 1
            source_count += 1
        copied[source_name] = source_count
    return {
        "ok": True,
        "root": str(target_root),
        "corpus_root": str(corpus_root),
        "copied": copied,
        "total_files": total,
        "skipped_sources": skipped_sources,
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


def import_runtime_facts(
    root: Path,
    *,
    stage: Path | None = None,
    das_url: str,
    router_url: str = "http://127.0.0.1:18180",
    retrieval_url: str = "http://127.0.0.1:18182",
    project: str,
    track: str | None,
    k1s_root: Path,
    phase_report: Path | None = None,
    workerbee_status: Path | None = None,
) -> dict[str, Any]:
    model_tracks = _load_json(root / "model-tracks.json")
    stage_dir = (stage or root / "stage").expanduser().resolve()
    selected_track = track or _stage_env_value(
        stage_dir / "manifests" / "ai-coordinator.yaml",
        "AI_FABRIC_TRACK",
    )
    selected_track = selected_track or str(model_tracks.get("default_track") or "")
    tracks = model_tracks.get("tracks") if isinstance(model_tracks.get("tracks"), dict) else {}
    config = tracks.get(selected_track)
    if not isinstance(config, dict):
        return {
            "ok": False,
            "findings": [
                _finding("error", "UNKNOWN_TRACK", f"unknown model track: {selected_track}")
            ],
        }
    facts = _runtime_facts(
        project=project,
        track=selected_track,
        config=config,
        k1s_root=k1s_root,
        stage_dir=stage_dir,
    )
    facts.extend(
        _runtime_state_facts(
            router_url=router_url,
            das_url=das_url,
            retrieval_url=retrieval_url,
            workerbee_status=workerbee_status,
        )
    )
    findings: list[dict[str, str]] = []
    if phase_report is not None:
        try:
            facts.extend(_phase_report_facts(_load_json(phase_report)))
        except ValueError as exc:
            findings.append(_finding("error", "PHASE_REPORT_INVALID", str(exc)))
    posted = {} if findings else _post_runtime_facts(das_url, facts, findings)
    return {
        "ok": not [item for item in findings if item["level"] == "error"],
        "das_url": das_url,
        "track": selected_track,
        "facts": facts,
        "posted": posted,
        "findings": findings,
    }


def import_phase_facts(*, phase_report: Path, das_url: str) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    try:
        facts = _phase_report_facts(_load_json(phase_report))
    except ValueError as exc:
        findings.append(_finding("error", "PHASE_REPORT_INVALID", str(exc)))
        facts = []
    posted = [] if findings else _post_facts(das_url, facts, findings)
    return {
        "ok": not [item for item in findings if item["level"] == "error"],
        "das_url": das_url,
        "phase_report": str(phase_report.expanduser().resolve()),
        "facts": facts,
        "posted": posted,
        "findings": findings,
    }


def emit_f5_evidence(
    root: Path,
    *,
    storage_root: Path | None = None,
    site_id: str,
    peer_site_id: str,
    project: str,
    track: str | None,
    query_id: str,
) -> dict[str, Any]:
    init_result = init_storage_layout(root, storage_root=storage_root)
    if not init_result.get("ok"):
        return init_result
    model_tracks = _load_json(root / "model-tracks.json")
    selected_track = track or str(model_tracks.get("default_track") or "")
    target_root = Path(str(init_result["root"]))
    payload = _f5_evidence_payload(
        storage_root=target_root,
        site_id=site_id,
        peer_site_id=peer_site_id,
        project=project,
        track=selected_track,
        query_id=query_id,
    )
    runs_dir = target_root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = runs_dir / "f5-evidence.json"
    evidence_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "ok": True,
        "root": str(target_root),
        "evidence_path": str(evidence_path),
        "evidence": payload,
        "facts": _f5_evidence_facts(payload),
        "findings": [],
    }


def validate_runtime(
    root: Path,
    *,
    suite: str,
    prompts: Path | None,
    storage_root: Path | None,
    run_id: str | None,
    track: str | None,
    router_url: str,
    das_url: str,
    retrieval_url: str,
    duration_seconds: int | None,
    workers: int | None,
    worker_sleep_seconds: float,
    gpu_sample_seconds: int | None,
    request_timeout: int,
    success_threshold: float,
    vram_growth_mib_max: int,
    workerbee_status: Path | None = None,
) -> dict[str, Any]:
    storage_layout = _load_json(root / "storage-layout.json")
    target_root = storage_root or Path(str(storage_layout["root"]))
    target_root = target_root.expanduser().resolve()
    selected_suites = _selected_runtime_suites(suite)
    selected_track = track or ("lora-adapter-smoke" if suite == "acceptance-closeout" else None)
    runtime_defaults = _runtime_defaults_for_suite(
        suite=suite,
        duration_seconds=duration_seconds,
        workers=workers,
        gpu_sample_seconds=gpu_sample_seconds,
    )
    duration_seconds = runtime_defaults["duration_seconds"]
    workers = runtime_defaults["workers"]
    gpu_sample_seconds = runtime_defaults["gpu_sample_seconds"]
    selected_run_id = run_id or f"runtime-validation-{_runtime_timestamp()}"
    run_dir = target_root / "runs" / selected_run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {name: run_dir / name for name in RUNTIME_OUTPUT_FILES}
    workerbee_status_payload = _workerbee_status_payload(workerbee_status)
    paths["workerbee-status.json"].write_text(
        json.dumps(workerbee_status_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    prompt_path = (prompts or root / "prompts" / "validation-suite.jsonl").expanduser().resolve()
    scenario_path = (
        prompts if suite == "advisor-scenarios" and prompts is not None else None
    ) or root / "prompts" / "advisor-scenarios.jsonl"
    scenario_path = scenario_path.expanduser().resolve()
    prompt_items = (
        _load_prompt_suite(prompt_path)
        if any(item in RUNTIME_PROMPT_SUITES for item in selected_suites)
        else []
    )
    advisor_scenarios = (
        _load_advisor_scenarios(scenario_path)
        if "advisor-scenarios" in selected_suites
        else []
    )
    findings: list[dict[str, str]] = []
    adapter_smoke_preflight = (
        _run_adapter_preflight(storage_root=target_root)
        if "lora-adapter-smoke" in selected_suites
        else None
    )
    lora_adapter_ready = (
        adapter_smoke_preflight is not None and adapter_smoke_preflight.get("state") == "ready"
    )
    requires_runtime = any(
        item in RUNTIME_ENDPOINT_SUITES
        and (item != "lora-adapter-smoke" or lora_adapter_ready)
        for item in selected_suites
    )
    requires_model_runtime = any(
        item in RUNTIME_MODEL_SUITES
        and (item != "lora-adapter-smoke" or lora_adapter_ready)
        for item in selected_suites
    )
    if requires_runtime:
        resolved = _resolve_runtime_endpoints(
            router_url=router_url,
            das_url=das_url,
            retrieval_url=retrieval_url,
            timeout_seconds=max(1, min(5, request_timeout)),
        )
        router_url = resolved["router_url"]
        das_url = resolved["das_url"]
        retrieval_url = resolved["retrieval_url"]
    health = (
        _health_snapshot(router_url=router_url, das_url=das_url, retrieval_url=retrieval_url)
        if requires_runtime
        else {
            "ok": True,
            "skipped": True,
            "reason": "selected suites do not require live runtime endpoints",
            "checked_at": _utc_now(),
        }
    )
    host_aliases = (
        _host_alias_snapshot(router_url=router_url, das_url=das_url, retrieval_url=retrieval_url)
        if requires_runtime
        else {
            "ok": True,
            "skipped": True,
            "reason": "selected suites do not require live runtime endpoints",
            "checked_at": _utc_now(),
            "endpoints": {},
        }
    )
    if requires_model_runtime and health.get("ok"):
        lane_readiness = _lane_readiness_snapshot(
            router_url=router_url,
            run_id=selected_run_id,
            timeout_seconds=min(max(request_timeout, 180), 300),
            request_timeout=min(max(request_timeout, 20), 60),
        )
    else:
        lane_readiness = {
            "ok": True,
            "skipped": True,
            "reason": (
                "selected suites do not require model-backed runtime lanes"
                if not requires_model_runtime
                else "runtime health failed before lane readiness"
            ),
            "checked_at": _utc_now(),
            "lanes": {},
        }
    paths["health.json"].write_text(
        json.dumps(health, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths["lane-readiness.json"].write_text(
        json.dumps(lane_readiness, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not health.get("ok"):
        findings.append(_finding("error", "RUNTIME_HEALTH", "one or more runtime endpoints failed"))
    if requires_model_runtime and not lane_readiness.get("ok"):
        findings.append(
            _finding("error", "RUNTIME_LANE_READINESS", "one or more model lanes were not ready")
        )
    if "recovery-smoke" in selected_suites and not host_aliases.get("ok"):
        findings.append(
            _finding("error", "RUNTIME_HOST_ALIASES", "one or more host aliases failed")
        )
    blocked_items: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "ok": True,
        "api_version": "workerbee.ai-fabric.runtime-validation/v1",
        "run_id": selected_run_id,
        "run_dir": str(run_dir),
        "suite": suite,
        "selected_suites": selected_suites,
        "track": selected_track,
        "created_at": _utc_now(),
        "router_url": router_url,
        "das_url": das_url,
        "retrieval_url": retrieval_url,
        "runtime_defaults": runtime_defaults,
        "output_files": {key: str(value) for key, value in paths.items()},
        "health": health,
        "host_aliases": host_aliases,
        "lane_readiness": lane_readiness,
        "blocked_items": blocked_items,
        "suites": {},
        "findings": findings,
    }
    for item in selected_suites:
        if item == "quality-contract":
            result = _run_quality_contract(
                prompts=prompt_items,
                router_url=router_url,
                run_id=selected_run_id,
                requests_path=paths["requests.jsonl"],
                request_timeout=request_timeout,
            )
        elif item == "quality-comparison":
            result = _run_quality_comparison(
                prompts=prompt_items,
                router_url=router_url,
                run_id=selected_run_id,
                requests_path=paths["requests.jsonl"],
                request_timeout=request_timeout,
            )
        elif item == "mixed-soak":
            result = _run_mixed_soak(
                prompts=prompt_items,
                router_url=router_url,
                run_id=selected_run_id,
                requests_path=paths["requests.jsonl"],
                gpu_samples_path=paths["gpu-samples.jsonl"],
                duration_seconds=duration_seconds,
                workers=workers,
                worker_sleep_seconds=worker_sleep_seconds,
                gpu_sample_seconds=gpu_sample_seconds,
                request_timeout=request_timeout,
                success_threshold=success_threshold,
                vram_growth_mib_max=vram_growth_mib_max,
            )
        elif item == "stress-burst":
            result = _run_stress_burst(
                prompts=prompt_items,
                router_url=router_url,
                run_id=selected_run_id,
                requests_path=paths["requests.jsonl"],
                gpu_samples_path=paths["gpu-samples.jsonl"],
                duration_seconds=duration_seconds,
                workers=workers,
                worker_sleep_seconds=worker_sleep_seconds,
                gpu_sample_seconds=gpu_sample_seconds,
                request_timeout=request_timeout,
                success_threshold=success_threshold,
                vram_growth_mib_max=vram_growth_mib_max,
            )
        elif item == "lora-plumbing":
            result = _run_lora_plumbing(
                root=root,
                storage_root=target_root,
                track=selected_track or "lora-plumbing",
                router_url=router_url,
                run_id=selected_run_id,
                requests_path=paths["requests.jsonl"],
                request_timeout=request_timeout,
            )
        elif item == "lora-adapter-smoke":
            result = _run_lora_adapter_smoke(
                storage_root=target_root,
                router_url=router_url,
                run_id=selected_run_id,
                requests_path=paths["requests.jsonl"],
                request_timeout=request_timeout,
                preflight=adapter_smoke_preflight,
            )
        elif item == "evidence-closeout":
            result = _run_evidence_closeout(
                das_url=das_url,
                f5_evidence_path=paths["f5-evidence.json"],
            )
        elif item == "adapter-preflight":
            result = _run_adapter_preflight(storage_root=target_root)
        elif item == "recovery-smoke":
            result = _run_recovery_smoke(
                router_url=router_url,
                run_id=selected_run_id,
                requests_path=paths["requests.jsonl"],
                request_timeout=request_timeout,
                host_aliases=host_aliases,
            )
        elif item == "advisor-scenarios":
            result = _run_advisor_scenarios(
                scenarios=advisor_scenarios,
                das_url=das_url,
                router_url=router_url,
                retrieval_url=retrieval_url,
                run_id=selected_run_id,
                output_path=paths["advisor-scenarios.json"],
                request_timeout=request_timeout,
            )
        else:  # pragma: no cover - argparse constrains values.
            result = {"ok": False, "findings": [_finding("error", "UNKNOWN_SUITE", item)]}
        summary["suites"][item] = result
        if result.get("state") == "blocked" or result.get("blocked") is True:
            blocked_items.append(
                {
                    "suite": item,
                    "state": str(result.get("state") or "blocked"),
                    "message": str(result.get("message") or ""),
                }
            )
        for finding in result.get("findings", []):
            if isinstance(finding, dict):
                findings.append(finding)
    summary["completed_at"] = _utc_now()
    summary["ok"] = not [item for item in findings if item.get("level") == "error"] and all(
        bool(item.get("ok")) for item in summary["suites"].values() if isinstance(item, dict)
    )
    runtime_profile = _ai_runtime_profile(
        root=root,
        summary=summary,
        runtime_profile_path=paths["ai-runtime-profile.json"],
    )
    paths["ai-runtime-profile.json"].write_text(
        json.dumps(runtime_profile, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    acceptance = _acceptance_run_summary(
        summary=summary,
        runtime_profile=runtime_profile,
        paths=paths,
    )
    paths["acceptance.json"].write_text(
        json.dumps(acceptance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    operator_report = _operator_report(
        summary=summary,
        acceptance=acceptance,
        runtime_profile=runtime_profile,
        workerbee_status=workerbee_status_payload,
    )
    paths["operator-report.json"].write_text(
        json.dumps(operator_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths["summary.json"].write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _selected_runtime_suites(suite: str) -> list[str]:
    if suite == "all":
        return ["quality-contract", "mixed-soak", "evidence-closeout"]
    if suite == "acceptance-closeout":
        return list(ACCEPTANCE_CLOSEOUT_SUITES)
    return [suite]


def _runtime_defaults_for_suite(
    *,
    suite: str,
    duration_seconds: int | None,
    workers: int | None,
    gpu_sample_seconds: int | None,
) -> dict[str, int]:
    if suite in {"stress-burst", "acceptance-closeout"}:
        return {
            "duration_seconds": duration_seconds or 900,
            "workers": workers or 6,
            "gpu_sample_seconds": gpu_sample_seconds or 15,
        }
    return {
        "duration_seconds": duration_seconds or 3600,
        "workers": workers or 3,
        "gpu_sample_seconds": gpu_sample_seconds or 30,
    }


def _ai_runtime_profile(
    *,
    root: Path,
    summary: dict[str, Any],
    runtime_profile_path: Path,
) -> dict[str, Any]:
    track_name, track_config = _runtime_profile_track(root=root, summary=summary)
    return {
        "api_version": AI_RUNTIME_PROFILE_API_VERSION,
        "kind": AI_RUNTIME_PROFILE_KIND,
        "run_id": str(summary.get("run_id") or ""),
        "track": track_name,
        "suite": str(summary.get("suite") or ""),
        "created_at": _utc_now(),
        "runtime_profile_path": str(runtime_profile_path),
        "authoritative": False,
        "controller_authority": "k1s",
        "model_lanes": _runtime_profile_model_lanes(track_config),
        "context_budget_tokens": _runtime_profile_context_budgets(track_config),
        "adapter_hotset": _runtime_profile_adapter_hotset(track_config, summary),
        "observed_vram_growth_mib": _runtime_profile_vram_growth(summary),
        "evidence": _runtime_profile_evidence(summary),
    }


def _runtime_profile_track(
    *,
    root: Path,
    summary: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    model_tracks = _load_json(root / "model-tracks.json")
    tracks = model_tracks.get("tracks") if isinstance(model_tracks.get("tracks"), dict) else {}
    default_track = str(model_tracks.get("default_track") or "baseline")
    track_name = str(summary.get("track") or default_track)
    track_config = tracks.get(track_name) if isinstance(tracks, dict) else None
    if not isinstance(track_config, dict):
        track_config = tracks.get(default_track) if isinstance(tracks, dict) else {}
        track_name = default_track
    return track_name, track_config if isinstance(track_config, dict) else {}


def _runtime_profile_model_lanes(track_config: dict[str, Any]) -> dict[str, Any]:
    lanes: dict[str, Any] = {}
    for lane in ("coordinator", "expert"):
        config = track_config.get(lane) if isinstance(track_config.get(lane), dict) else {}
        lanes[lane] = {
            "lane": lane,
            "model": config.get("model"),
            "revision": config.get("revision"),
            "served_model_name": config.get("served_model_name") or _expected_chat_model(lane),
            "context_budget_tokens": _int_or_none(config.get("max_model_len")),
            "gpu_memory_utilization": _float_or_none(config.get("gpu_memory_utilization")),
            "quantization": config.get("quantization"),
            "lora_enabled": bool(config.get("enable_lora")),
        }
    return lanes


def _runtime_profile_context_budgets(track_config: dict[str, Any]) -> dict[str, int | None]:
    budgets: dict[str, int | None] = {}
    for lane in ("coordinator", "expert"):
        config = track_config.get(lane) if isinstance(track_config.get(lane), dict) else {}
        budgets[lane] = _int_or_none(config.get("max_model_len"))
    return budgets


def _runtime_profile_adapter_hotset(
    track_config: dict[str, Any],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    suites = summary.get("suites") if isinstance(summary.get("suites"), dict) else {}
    adapter_result = suites.get("lora-adapter-smoke") if isinstance(suites, dict) else {}
    preflight = (
        adapter_result.get("preflight")
        if isinstance(adapter_result, dict) and isinstance(adapter_result.get("preflight"), dict)
        else suites.get("adapter-preflight")
    )
    preflight_state = (
        str(preflight.get("state"))
        if isinstance(preflight, dict) and preflight.get("state") is not None
        else "unknown"
    )
    hotset: list[dict[str, Any]] = []
    for lane in ("coordinator", "expert"):
        config = track_config.get(lane) if isinstance(track_config.get(lane), dict) else {}
        modules = config.get("lora_modules") if isinstance(config.get("lora_modules"), list) else []
        for module in modules:
            if not isinstance(module, dict):
                continue
            hotset.append(
                {
                    "lane": lane,
                    "name": module.get("name"),
                    "path": module.get("path"),
                    "base_model_name": module.get("base_model_name"),
                    "max_lora_rank": _int_or_none(module.get("max_lora_rank")),
                    "state": preflight_state,
                    "claim_scope": "runtime-smoke-only",
                }
            )
    return hotset


def _runtime_profile_vram_growth(summary: dict[str, Any]) -> int | None:
    suites = summary.get("suites") if isinstance(summary.get("suites"), dict) else {}
    for name in ("stress-burst", "mixed-soak"):
        result = suites.get(name)
        if isinstance(result, dict) and isinstance(result.get("final_vram_growth_mib"), int):
            return int(result["final_vram_growth_mib"])
    return None


def _runtime_profile_evidence(summary: dict[str, Any]) -> dict[str, Any]:
    health = summary.get("health") if isinstance(summary.get("health"), dict) else {}
    endpoints = health.get("endpoints") if isinstance(health.get("endpoints"), dict) else {}
    das = endpoints.get("das") if isinstance(endpoints.get("das"), dict) else {}
    retrieval = endpoints.get("retrieval") if isinstance(endpoints.get("retrieval"), dict) else {}
    output_files = (
        summary.get("output_files") if isinstance(summary.get("output_files"), dict) else {}
    )
    evidence: dict[str, Any] = {
        "runtime_validation_ref": output_files.get("summary.json"),
        "workerbee_status_ref": output_files.get("workerbee-status.json"),
        "f5_evidence_ref": output_files.get("f5-evidence.json"),
        "advisor_scenarios_ref": output_files.get("advisor-scenarios.json"),
        "das_fact_count": _int_or_none(das.get("fact_count")),
        "das_f5_evidence_count": _int_or_none(das.get("f5_evidence_count")),
        "retrieval_corpus_count": {
            "document_count": _int_or_none(retrieval.get("document_count")),
            "chunk_count": _int_or_none(retrieval.get("chunk_count")),
        },
        "advisory_trace_refs": _collect_advisory_trace_refs(summary.get("suites")),
    }
    soak = _runtime_profile_soak_evidence(summary)
    if soak is not None:
        evidence["soak"] = soak
    return evidence


def _runtime_profile_soak_evidence(summary: dict[str, Any]) -> dict[str, Any] | None:
    suites = summary.get("suites") if isinstance(summary.get("suites"), dict) else {}
    result = suites.get("mixed-soak")
    if not isinstance(result, dict):
        return None
    duration_seconds = _int_or_none(result.get("duration_seconds"))
    promotion_ready = (
        result.get("ok") is True
        and duration_seconds is not None
        and duration_seconds >= SOAK_PROMOTION_DURATION_SECONDS
    )
    return {
        "suite": "mixed-soak",
        "track": str(summary.get("track") or ""),
        "ok": bool(result.get("ok")),
        "duration_seconds": duration_seconds,
        "workers": _int_or_none(result.get("workers")),
        "request_count": _int_or_none(result.get("request_count")),
        "success_rate": _float_or_none(result.get("success_rate")),
        "gpu_sample_count": _int_or_none(result.get("gpu_sample_count")),
        "final_vram_growth_mib": _int_or_none(result.get("final_vram_growth_mib")),
        "vram_growth_mib_max": _int_or_none(result.get("vram_growth_mib_max")),
        "promotion_duration_seconds": SOAK_PROMOTION_DURATION_SECONDS,
        "promotion_ready": promotion_ready,
    }


def _workerbee_status_payload(workerbee_status: Path | None) -> dict[str, Any]:
    if workerbee_status is None:
        return _normalize_workerbee_status_payload(
            {
                "ok": None,
                "note": "capture WorkerBee project_status after runtime validation",
                "created_at": _utc_now(),
            }
        )
    return _normalize_workerbee_status_payload(_load_json(workerbee_status.expanduser().resolve()))


def _normalize_workerbee_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("data"), dict) and payload.get("kind") == "ProjectStatus":
        data = payload["data"]
        ok = payload.get("ok")
        if ok is None:
            ok = _workerbee_project_status_ok(data)
        return {
            "api_version": str(payload.get("api_version") or "workerbee.mcp/v1"),
            "kind": "ProjectStatus",
            "ok": ok,
            "project": payload.get("project"),
            "data": data,
            "source": "workerbee.mcp.project_status",
        }

    project_status = payload.get("project_status")
    if isinstance(project_status, dict):
        return {
            "api_version": "workerbee.mcp/v1",
            "kind": "ProjectStatus",
            "ok": _workerbee_project_status_ok(project_status),
            "project": payload.get("project"),
            "data": project_status,
            "source": "workerbee.cli.project_status",
        }

    return {
        "api_version": "workerbee.mcp/v1",
        "kind": "ProjectStatus",
        "ok": payload.get("ok"),
        "project": payload.get("project"),
        "data": payload.get("data") if isinstance(payload.get("data"), dict) else {},
        "note": payload.get("note"),
        "created_at": payload.get("created_at"),
        "source": str(payload.get("source") or "workerbee.status.placeholder"),
    }


def _workerbee_project_status_ok(status: dict[str, Any]) -> bool:
    app_status = status.get("app_status") if isinstance(status.get("app_status"), dict) else {}
    degraded = _int_or_none(app_status.get("degraded_workload_count"))
    return bool(
        status.get("running")
        and app_status.get("ready")
        and (degraded is None or degraded == 0)
    )


def _collect_advisory_trace_refs(value: Any) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            trace_id = item.get("trace_id")
            trace_path = item.get("trace_path")
            if isinstance(trace_id, str) or isinstance(trace_path, str):
                key = (str(trace_id or ""), str(trace_path or ""))
                if key not in seen:
                    seen.add(key)
                    refs.append(
                        {
                            "trace_id": str(trace_id or ""),
                            "trace_path": str(trace_path or ""),
                        }
                    )
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return refs


def _acceptance_run_summary(
    *,
    summary: dict[str, Any],
    runtime_profile: dict[str, Any],
    paths: dict[str, Path],
) -> dict[str, Any]:
    suites = summary.get("suites") if isinstance(summary.get("suites"), dict) else {}
    suite_status = []
    missing = []
    for suite_name in ACCEPTANCE_CLOSEOUT_SUITES:
        result = suites.get(suite_name) if isinstance(suites, dict) else None
        if not isinstance(result, dict):
            missing.append(suite_name)
            suite_status.append({"suite": suite_name, "ok": False, "state": "missing"})
            continue
        suite_status.append(
            {
                "suite": suite_name,
                "ok": bool(result.get("ok")),
                "state": str(result.get("state") or ("passed" if result.get("ok") else "failed")),
            }
        )
    is_acceptance = summary.get("suite") == "acceptance-closeout"
    error_findings = [
        item
        for item in summary.get("findings", [])
        if isinstance(item, dict) and item.get("level") == "error"
    ]
    blocked_items = (
        summary.get("blocked_items") if isinstance(summary.get("blocked_items"), list) else []
    )
    ok = bool(
        is_acceptance
        and summary.get("ok")
        and not missing
        and not blocked_items
        and not error_findings
        and all(item["ok"] for item in suite_status)
    )
    return {
        "api_version": ACCEPTANCE_RUN_API_VERSION,
        "kind": "AIFabricAcceptanceRun",
        "run_id": str(summary.get("run_id") or ""),
        "suite": str(summary.get("suite") or ""),
        "track": runtime_profile.get("track"),
        "acceptance": is_acceptance,
        "ok": ok,
        "created_at": summary.get("created_at"),
        "completed_at": summary.get("completed_at"),
        "required_suites": list(ACCEPTANCE_CLOSEOUT_SUITES),
        "suite_status": suite_status,
        "missing_suites": missing,
        "blocked_items": blocked_items,
        "findings": summary.get("findings", []),
        "runtime_profile_ref": str(paths["ai-runtime-profile.json"]),
        "artifacts": {name: str(path) for name, path in sorted(paths.items())},
    }


def _operator_report(
    *,
    summary: dict[str, Any],
    acceptance: dict[str, Any],
    runtime_profile: dict[str, Any],
    workerbee_status: dict[str, Any],
) -> dict[str, Any]:
    suite_status = acceptance.get("suite_status")
    validation = suite_status if isinstance(suite_status, list) else []
    gaps = [
        "LoRA adapter payload is validated only as a runtime smoke adapter.",
        "k1s scheduler/admission behavior does not consume this runtime profile yet.",
    ]
    if workerbee_status.get("ok") is not True:
        gaps.append(
            "Final WorkerBee MCP project status should be refreshed in workerbee-status.json before promotion."
        )
    evidence = (
        runtime_profile.get("evidence")
        if isinstance(runtime_profile.get("evidence"), dict)
        else {}
    )
    soak = evidence.get("soak") if isinstance(evidence, dict) else None
    track = str(runtime_profile.get("track") or "")
    if track in {"baseline", "quality"}:
        if not isinstance(soak, dict) or soak.get("ok") is not True:
            gaps.append("Baseline or quality soak evidence is missing or not passing.")
        elif soak.get("promotion_ready") is not True:
            duration = _int_or_none(soak.get("duration_seconds"))
            threshold = _int_or_none(soak.get("promotion_duration_seconds"))
            gaps.append(
                "Baseline or quality soak evidence is present but "
                f"{duration or 0}s is below the {threshold or SOAK_PROMOTION_DURATION_SECONDS}s "
                "promotion threshold."
            )
    return {
        "api_version": OPERATOR_REPORT_API_VERSION,
        "kind": "AIFabricOperatorReport",
        "run_id": str(summary.get("run_id") or ""),
        "stage": "examples/ai-fabric-lab/stage-lora-adapter-smoke",
        "track": runtime_profile.get("track"),
        "ok": bool(acceptance.get("ok")) if acceptance.get("acceptance") else bool(summary.get("ok")),
        "validation": validation,
        "known_gaps": gaps,
        "recommended_next_action": (
            "promote the ai-runtime-profile contract into k1s scheduling/admission design"
            if acceptance.get("ok")
            else "resolve failed or blocked validation suites before fabric contract promotion"
        ),
        "runtime_profile_ref": acceptance.get("runtime_profile_ref"),
    }


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_prompt_suite(path: Path) -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_no} must be a JSON object")
        prompt_id = str(payload.get("id") or "")
        lane = str(payload.get("lane") or "")
        suite = str(payload.get("suite") or "")
        prompt = str(payload.get("prompt") or "")
        if not prompt_id or prompt_id in seen:
            raise ValueError(f"{path}:{line_no} has missing or duplicate id")
        if lane not in {"coordinator", "expert"}:
            raise ValueError(f"{path}:{line_no} has invalid lane")
        if suite not in {"quality-contract", "mixed-soak"}:
            raise ValueError(f"{path}:{line_no} has invalid suite")
        if not prompt.strip():
            raise ValueError(f"{path}:{line_no} has empty prompt")
        seen.add(prompt_id)
        prompts.append(payload)
    return prompts


def _load_advisor_scenarios(path: Path) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_no} must be a JSON object")
        scenario_id = str(payload.get("id") or "")
        subject = str(payload.get("subject") or "")
        intent = str(payload.get("intent") or "")
        query = str(payload.get("query") or "")
        facts = payload.get("facts")
        expect = payload.get("expect")
        if not scenario_id or scenario_id in seen:
            raise ValueError(f"{path}:{line_no} has missing or duplicate id")
        if not subject.strip():
            raise ValueError(f"{path}:{line_no} has empty subject")
        if not intent.strip():
            raise ValueError(f"{path}:{line_no} has empty intent")
        if not query.strip():
            raise ValueError(f"{path}:{line_no} has empty query")
        if not isinstance(facts, list) or any(not isinstance(item, dict) for item in facts):
            raise ValueError(f"{path}:{line_no} has invalid facts")
        if not isinstance(expect, dict):
            raise ValueError(f"{path}:{line_no} has invalid expect")
        if str(expect.get("status") or "") not in {"review", "blocked"}:
            raise ValueError(f"{path}:{line_no} has invalid expected status")
        for list_key in ("risks_present", "risks_absent"):
            value = expect.get(list_key, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{path}:{line_no} has invalid {list_key}")
        for int_key in ("min_evidence_refs", "blocked_condition_count"):
            if int_key in expect:
                value = expect[int_key]
                if not isinstance(value, int) or value < 0:
                    raise ValueError(f"{path}:{line_no} has invalid {int_key}")
        seen.add(scenario_id)
        scenarios.append(payload)
    if not scenarios:
        raise ValueError(f"{path} did not contain advisor scenarios")
    return scenarios


def _resolve_runtime_endpoints(
    *,
    router_url: str,
    das_url: str,
    retrieval_url: str,
    timeout_seconds: int,
) -> dict[str, str]:
    return {
        "router_url": _resolve_runtime_endpoint(router_url, "router", timeout_seconds),
        "das_url": _resolve_runtime_endpoint(das_url, "das", timeout_seconds),
        "retrieval_url": _resolve_runtime_endpoint(
            retrieval_url, "retrieval", timeout_seconds
        ),
    }


def _resolve_runtime_endpoint(url: str, role: str, timeout_seconds: int) -> str:
    for candidate in _runtime_url_candidates(url=url, role=role):
        if _endpoint_ok(candidate, timeout_seconds=timeout_seconds):
            return candidate
    return url


def _runtime_url_candidates(*, url: str, role: str) -> list[str]:
    candidates = [url]
    parsed = urlparse(url)
    fallback = RUNTIME_URL_FALLBACKS.get(role)
    if parsed.hostname and _is_local_url(url) and fallback is not None:
        scheme = parsed.scheme or "http"
        candidates.append(f"{scheme}://{fallback['service_name']}:{fallback['container_port']}")
    return candidates


def _endpoint_ok(url: str, timeout_seconds: int) -> bool:
    return bool(_get_json(f"{url.rstrip('/')}/healthz", timeout=timeout_seconds).get("ok"))


def _health_snapshot(*, router_url: str, das_url: str, retrieval_url: str) -> dict[str, Any]:
    endpoints = {
        "router": f"{router_url.rstrip('/')}/healthz",
        "das": f"{das_url.rstrip('/')}/healthz",
        "retrieval": f"{retrieval_url.rstrip('/')}/healthz",
    }
    results = {name: _get_json(url, timeout=20) for name, url in endpoints.items()}
    return {
        "ok": all(bool(item.get("ok")) for item in results.values()),
        "checked_at": _utc_now(),
        "endpoints": results,
    }


def _host_alias_snapshot(*, router_url: str, das_url: str, retrieval_url: str) -> dict[str, Any]:
    endpoints = {
        "router": f"{router_url.rstrip('/')}/healthz",
        "das": f"{das_url.rstrip('/')}/healthz",
        "retrieval": f"{retrieval_url.rstrip('/')}/healthz",
    }
    results = {}
    for name, url in endpoints.items():
        payload = _get_json(url, timeout=20)
        results[name] = {
            "url": url,
            "ok": bool(payload.get("ok")),
            "service": payload.get("service"),
            "error": payload.get("error"),
        }
    return {
        "ok": all(bool(item.get("ok")) for item in results.values()),
        "checked_at": _utc_now(),
        "endpoints": results,
    }


def _lane_readiness_snapshot(
    *,
    router_url: str,
    run_id: str,
    timeout_seconds: int,
    request_timeout: int,
    interval_seconds: float = 5.0,
) -> dict[str, Any]:
    lanes = {
        "coordinator": {
            "ok": False,
            "attempts": 0,
            "expected_model": _expected_chat_model("coordinator"),
        },
        "expert": {
            "ok": False,
            "attempts": 0,
            "expected_model": _expected_chat_model("expert"),
        },
    }
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        for lane, state in lanes.items():
            if state.get("ok"):
                continue
            attempt = _chat_lane_readiness_probe(
                lane=lane,
                router_url=router_url,
                run_id=run_id,
                request_timeout=request_timeout,
            )
            state.update(attempt)
            state["attempts"] = int(state.get("attempts") or 0) + 1
        if all(bool(item.get("ok")) for item in lanes.values()):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval_seconds, remaining))
    return {
        "ok": all(bool(item.get("ok")) for item in lanes.values()),
        "checked_at": _utc_now(),
        "timeout_seconds": timeout_seconds,
        "request_timeout": request_timeout,
        "lanes": lanes,
    }


def _chat_lane_readiness_probe(
    *,
    lane: str,
    router_url: str,
    run_id: str,
    request_timeout: int,
) -> dict[str, Any]:
    payload = {
        "lane": lane,
        "messages": [{"role": "user", "content": "Readiness check. Reply ok."}],
        "temperature": 0,
        "max_tokens": 4,
        "metadata": {
            "run_id": run_id,
            "suite": "lane-readiness",
            "lane": lane,
        },
    }
    started = time.monotonic()
    response = _post_json(
        f"{router_url.rstrip('/')}/v1/chat/completions",
        payload,
        timeout=request_timeout,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    data = response.get("json") if isinstance(response.get("json"), dict) else {}
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    content = None
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
    expected_model = _expected_chat_model(lane)
    model_id = data.get("model")
    checks = {
        "http_ok": response.get("status") == 200,
        "expected_model": model_id == expected_model,
        "content_present": isinstance(content, str) and bool(content.strip()),
    }
    return {
        "ok": all(checks.values()),
        "status": response.get("status"),
        "elapsed_ms": elapsed_ms,
        "expected_model": expected_model,
        "model_id": model_id,
        "content_chars": len(content or ""),
        "checks": checks,
        "error": response.get("error"),
        "checked_at": _utc_now(),
    }


def _run_quality_contract(
    *,
    prompts: list[dict[str, Any]],
    router_url: str,
    run_id: str,
    requests_path: Path,
    request_timeout: int,
    suite_label: str = "quality-contract",
) -> dict[str, Any]:
    selected = [item for item in prompts if item.get("suite") == "quality-contract"]
    findings: list[dict[str, str]] = []
    results = []
    for prompt in selected:
        record = _advisory_prompt_record(
            prompt=prompt,
            router_url=router_url,
            run_id=run_id,
            request_timeout=request_timeout,
            suite_label=suite_label,
        )
        _append_jsonl(requests_path, record)
        results.append(record)
        if not record.get("ok"):
            findings.append(
                _finding(
                    "error",
                    "QUALITY_PROMPT_FAILED",
                    str(record.get("id") or "unknown prompt"),
                )
            )
    return {
        "ok": bool(selected) and not [item for item in findings if item["level"] == "error"],
        "suite_label": suite_label,
        "prompt_suite": "quality-contract",
        "prompt_count": len(selected),
        "results": results,
        "findings": findings,
    }


def _run_quality_comparison(
    *,
    prompts: list[dict[str, Any]],
    router_url: str,
    run_id: str,
    requests_path: Path,
    request_timeout: int,
) -> dict[str, Any]:
    result = _run_quality_contract(
        prompts=prompts,
        router_url=router_url,
        run_id=run_id,
        requests_path=requests_path,
        request_timeout=request_timeout,
        suite_label="quality-comparison",
    )
    result["metrics"] = _quality_metrics(result["results"])
    return result


def _quality_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    lanes: dict[str, dict[str, Any]] = {}
    for record in results:
        lane = str(record.get("expected_lane") or record.get("lane") or "unknown")
        lane_metrics = lanes.setdefault(
            lane,
            {
                "prompt_count": 0,
                "ok_count": 0,
                "elapsed_ms_total": 0,
                "retrieval_hits_min": None,
                "symbolic_facts_min": None,
                "models": set(),
            },
        )
        lane_metrics["prompt_count"] += 1
        if record.get("ok"):
            lane_metrics["ok_count"] += 1
        if isinstance(record.get("elapsed_ms"), int):
            lane_metrics["elapsed_ms_total"] += int(record["elapsed_ms"])
        if record.get("model_id"):
            lane_metrics["models"].add(str(record["model_id"]))
        for source_key, target_key in (
            ("retrieval_hits", "retrieval_hits_min"),
            ("symbolic_facts", "symbolic_facts_min"),
        ):
            value = record.get(source_key)
            if isinstance(value, int):
                current = lane_metrics[target_key]
                lane_metrics[target_key] = value if current is None else min(current, value)
    serializable = {}
    for lane, metrics in lanes.items():
        count = int(metrics["prompt_count"])
        serializable[lane] = {
            "prompt_count": count,
            "ok_count": int(metrics["ok_count"]),
            "avg_elapsed_ms": int(metrics["elapsed_ms_total"] / count) if count else None,
            "retrieval_hits_min": metrics["retrieval_hits_min"],
            "symbolic_facts_min": metrics["symbolic_facts_min"],
            "models": sorted(metrics["models"]),
        }
    return {"lanes": serializable}


def _advisory_prompt_record(
    *,
    prompt: dict[str, Any],
    router_url: str,
    run_id: str,
    request_timeout: int,
    suite_label: str = "quality-contract",
) -> dict[str, Any]:
    expected_lane = str(prompt.get("lane"))
    payload = {
        "query": str(prompt["prompt"]),
        "lane": expected_lane,
        "run_id": run_id,
        "metadata": {
            "prompt_id": str(prompt["id"]),
            "suite": suite_label,
        },
    }
    started = time.monotonic()
    response = _post_json(
        f"{router_url.rstrip('/')}/v1/advisory/query",
        payload,
        timeout=request_timeout,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    data = response.get("json") if isinstance(response.get("json"), dict) else {}
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    retrieval = evidence.get("retrieval") if isinstance(evidence.get("retrieval"), dict) else {}
    symbolic = evidence.get("symbolic") if isinstance(evidence.get("symbolic"), dict) else {}
    retrieval_results = (
        retrieval.get("results") if isinstance(retrieval.get("results"), list) else []
    )
    symbolic_results = symbolic.get("results") if isinstance(symbolic.get("results"), list) else []
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    raw_model = model.get("raw") if isinstance(model.get("raw"), dict) else {}
    trace = data.get("decision_trace") if isinstance(data.get("decision_trace"), dict) else {}
    trace_retrieval = trace.get("retrieval") if isinstance(trace.get("retrieval"), dict) else {}
    trace_symbolic = trace.get("symbolic") if isinstance(trace.get("symbolic"), dict) else {}
    trace_retrieval_results = (
        trace_retrieval.get("results")
        if isinstance(trace_retrieval.get("results"), list)
        else []
    )
    trace_symbolic_results = (
        trace_symbolic.get("results")
        if isinstance(trace_symbolic.get("results"), list)
        else []
    )
    min_retrieval_hits = int(prompt.get("min_retrieval_hits") or 1)
    min_symbolic_facts = int(prompt.get("min_symbolic_facts") or 1)
    checks = {
        "http_ok": response.get("status") == 200,
        "expected_lane": data.get("lane") == expected_lane,
        "model_ok": bool(model.get("ok")),
        "retrieval_count": len(retrieval_results) >= min_retrieval_hits,
        "symbolic_count": len(symbolic_results) >= min_symbolic_facts,
        "authoritative_false": data.get("authoritative") is False,
        "trace_persisted": bool(data.get("trace_id")) and bool(data.get("trace_path")),
        "trace_selected_lane": trace.get("selected_lane") == expected_lane,
        "trace_retrieval_packet": len(trace_retrieval_results) >= min_retrieval_hits,
        "trace_symbolic_packet": len(trace_symbolic_results) >= min_symbolic_facts,
    }
    return {
        "id": str(prompt["id"]),
        "suite": suite_label,
        "status": response.get("status"),
        "elapsed_ms": elapsed_ms,
        "lane": data.get("lane"),
        "expected_lane": expected_lane,
        "model_id": raw_model.get("model"),
        "model_ok": model.get("ok"),
        "model_error": model.get("error"),
        "retrieval_hits": len(retrieval_results),
        "symbolic_facts": len(symbolic_results),
        "authoritative": data.get("authoritative"),
        "trace_id": data.get("trace_id"),
        "trace_path": data.get("trace_path"),
        "trace_retrieval_hits": len(trace_retrieval_results),
        "trace_symbolic_facts": len(trace_symbolic_results),
        "checks": checks,
        "ok": all(checks.values()),
        "error": response.get("error"),
        "recorded_at": _utc_now(),
    }


def _run_mixed_soak(
    *,
    prompts: list[dict[str, Any]],
    router_url: str,
    run_id: str,
    requests_path: Path,
    gpu_samples_path: Path,
    duration_seconds: int,
    workers: int,
    worker_sleep_seconds: float,
    gpu_sample_seconds: int,
    request_timeout: int,
    success_threshold: float,
    vram_growth_mib_max: int,
    suite_label: str = "mixed-soak",
) -> dict[str, Any]:
    selected = [item for item in prompts if item.get("suite") == "mixed-soak"]
    if not selected:
        return {"ok": False, "findings": [_finding("error", "SOAK_PROMPTS", "no soak prompts")]}
    workers = max(1, workers)
    duration_seconds = max(1, duration_seconds)
    gpu_sample_seconds = max(1, gpu_sample_seconds)
    findings: list[dict[str, str]] = []
    records: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    lock = threading.Lock()
    stop_at = time.monotonic() + duration_seconds
    stop_event = threading.Event()

    def sample_gpu() -> None:
        while not stop_event.is_set():
            sample = _gpu_sample()
            with lock:
                samples.append(sample)
            _append_jsonl(gpu_samples_path, sample)
            sleep_for = min(gpu_sample_seconds, max(0.0, stop_at - time.monotonic()))
            if sleep_for <= 0:
                return
            stop_event.wait(sleep_for)

    def run_worker(worker_id: int) -> None:
        index = worker_id
        while time.monotonic() < stop_at:
            prompt = selected[index % len(selected)]
            index += workers
            record = _chat_prompt_record(
                prompt=prompt,
                router_url=router_url,
                run_id=run_id,
                worker_id=worker_id,
                request_timeout=request_timeout,
                suite_label=suite_label,
            )
            with lock:
                records.append(record)
            _append_jsonl(requests_path, record)
            if worker_sleep_seconds > 0:
                stop_event.wait(min(worker_sleep_seconds, max(0.0, stop_at - time.monotonic())))

    sampler = threading.Thread(target=sample_gpu, name="ai-fabric-gpu-sampler", daemon=True)
    sampler.start()
    threads = [
        threading.Thread(target=run_worker, args=(worker_id,), name=f"ai-fabric-soak-{worker_id}")
        for worker_id in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stop_event.set()
    sampler.join(timeout=max(1, gpu_sample_seconds))
    if not samples:
        sample = _gpu_sample()
        samples.append(sample)
        _append_jsonl(gpu_samples_path, sample)
    total = len(records)
    ok_count = len([item for item in records if item.get("ok")])
    success_rate = ok_count / total if total else 0.0
    final_growth = _final_vram_growth_mib(samples, window_seconds=900)
    no_oom = not any("out of memory" in str(item.get("error") or "").lower() for item in records)
    checks = {
        "requests_present": total > 0,
        "success_rate": success_rate >= success_threshold,
        "gpu_samples_present": bool(samples),
        "final_vram_growth": final_growth is not None and final_growth <= vram_growth_mib_max,
        "no_oom": no_oom,
    }
    if not checks["success_rate"]:
        findings.append(
            _finding(
                "error",
                "SOAK_SUCCESS_RATE",
                f"success rate {success_rate:.3f} below {success_threshold:.3f}",
            )
        )
    if not checks["final_vram_growth"]:
        findings.append(
            _finding(
                "error",
                "SOAK_VRAM_GROWTH",
                f"final VRAM growth {final_growth} MiB exceeds {vram_growth_mib_max} MiB",
            )
        )
    if not no_oom:
        findings.append(_finding("error", "SOAK_OOM", "one or more requests reported OOM"))
    return {
        "ok": all(checks.values()),
        "duration_seconds": duration_seconds,
        "workers": workers,
        "request_count": total,
        "ok_count": ok_count,
        "success_rate": success_rate,
        "gpu_sample_count": len(samples),
        "final_vram_growth_mib": final_growth,
        "vram_growth_mib_max": vram_growth_mib_max,
        "checks": checks,
        "findings": findings,
    }


def _run_stress_burst(
    *,
    prompts: list[dict[str, Any]],
    router_url: str,
    run_id: str,
    requests_path: Path,
    gpu_samples_path: Path,
    duration_seconds: int,
    workers: int,
    worker_sleep_seconds: float,
    gpu_sample_seconds: int,
    request_timeout: int,
    success_threshold: float,
    vram_growth_mib_max: int,
) -> dict[str, Any]:
    result = _run_mixed_soak(
        prompts=prompts,
        router_url=router_url,
        run_id=run_id,
        requests_path=requests_path,
        gpu_samples_path=gpu_samples_path,
        duration_seconds=duration_seconds,
        workers=workers,
        worker_sleep_seconds=worker_sleep_seconds,
        gpu_sample_seconds=gpu_sample_seconds,
        request_timeout=request_timeout,
        success_threshold=success_threshold,
        vram_growth_mib_max=vram_growth_mib_max,
        suite_label="stress-burst",
    )
    result["profile"] = "stress-burst"
    result["suite_label"] = "stress-burst"
    return result


def _chat_prompt_record(
    *,
    prompt: dict[str, Any],
    router_url: str,
    run_id: str,
    worker_id: int,
    request_timeout: int,
    suite_label: str = "mixed-soak",
    model_override: str | None = None,
    expected_model: str | None = None,
) -> dict[str, Any]:
    lane = str(prompt.get("lane"))
    payload = {
        "lane": lane,
        "messages": [{"role": "user", "content": str(prompt["prompt"])}],
        "temperature": 0,
        "max_tokens": int(prompt.get("max_tokens") or 32),
        "metadata": {"run_id": run_id, "prompt_id": str(prompt["id"]), "worker_id": worker_id},
    }
    if model_override:
        payload["model"] = model_override
    started = time.monotonic()
    response = _post_json(
        f"{router_url.rstrip('/')}/v1/chat/completions",
        payload,
        timeout=request_timeout,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    data = response.get("json") if isinstance(response.get("json"), dict) else {}
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    content = None
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
    model_id = data.get("model")
    expected_model = expected_model or model_override or _expected_chat_model(lane)
    checks = {
        "http_ok": response.get("status") == 200,
        "expected_model": model_id == expected_model,
        "content_present": isinstance(content, str) and bool(content.strip()),
    }
    return {
        "id": str(prompt["id"]),
        "suite": suite_label,
        "worker_id": worker_id,
        "status": response.get("status"),
        "elapsed_ms": elapsed_ms,
        "lane": lane,
        "model_id": model_id,
        "expected_model": expected_model,
        "model_override": model_override,
        "content_chars": len(content or ""),
        "checks": checks,
        "ok": all(checks.values()),
        "error": response.get("error"),
        "recorded_at": _utc_now(),
    }


def _expected_chat_model(lane: str) -> str:
    return "k1s-code-expert" if lane == "expert" else "general-coordinator"


def _run_adapter_preflight(
    *,
    storage_root: Path,
    adapter_relative_path: str = ADAPTER_VALIDATION_RELATIVE_PATH,
) -> dict[str, Any]:
    adapter_path = storage_root / adapter_relative_path
    weight_candidates = [
        adapter_path / "adapter_model.safetensors",
        adapter_path / "adapter_model.bin",
        adapter_path / "adapter_model.pt",
    ]
    config_path = adapter_path / "adapter_config.json"
    weight_path = next((path for path in weight_candidates if path.is_file()), None)
    artifact_checks = {
        "path_exists": adapter_path.exists(),
        "is_directory": adapter_path.is_dir(),
        "adapter_config_present": config_path.is_file(),
        "adapter_weights_present": weight_path is not None,
    }
    if not all(artifact_checks.values()):
        missing = [key for key, value in artifact_checks.items() if not value]
        return {
            "ok": True,
            "state": "blocked",
            "blocked": True,
            "adapter_path": str(adapter_path),
            "expected_files": [
                "adapter_config.json",
                "adapter_model.safetensors|adapter_model.bin|adapter_model.pt",
            ],
            "checks": artifact_checks,
            "missing": missing,
            "message": "validation adapter payload is not present",
            "findings": [
                _finding(
                    "warning",
                    "ADAPTER_PREFLIGHT_BLOCKED",
                    f"missing validation adapter payload at {adapter_path}",
                )
            ],
        }

    config: dict[str, Any] = {}
    config_error = ""
    try:
        config = _load_json(config_path)
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        config_error = str(exc)
    base_model = _adapter_base_model(config)
    rank = _adapter_rank(config)
    target_modules = config.get("target_modules")
    metadata_checks = {
        "adapter_config_valid": not config_error,
        "adapter_base_model_expected": base_model in ADAPTER_EXPECTED_BASE_MODELS,
        "adapter_rank_valid": rank is not None and 0 < rank <= ADAPTER_MAX_LORA_RANK,
        "adapter_target_modules_present": _adapter_target_modules_present(target_modules),
    }
    checks = {**artifact_checks, **metadata_checks}
    metadata = {
        "base_model_name": base_model,
        "rank": rank,
        "target_modules": target_modules,
        "adapter_model": str(weight_path) if weight_path else None,
        "config_error": config_error or None,
        "expected_base_models": list(ADAPTER_EXPECTED_BASE_MODELS),
        "max_lora_rank": ADAPTER_MAX_LORA_RANK,
    }
    if all(checks.values()):
        return {
            "ok": True,
            "state": "ready",
            "blocked": False,
            "adapter_path": str(adapter_path),
            "checks": checks,
            "metadata": metadata,
            "findings": [],
        }
    invalid = [key for key, value in metadata_checks.items() if not value]
    findings = [
        _finding("error", "ADAPTER_PREFLIGHT_INVALID", ",".join(invalid) or "metadata")
    ]
    return {
        "ok": False,
        "state": "invalid",
        "blocked": False,
        "adapter_path": str(adapter_path),
        "checks": checks,
        "metadata": metadata,
        "invalid": invalid,
        "message": "validation adapter payload is present but does not match expectations",
        "findings": findings,
    }


def _adapter_base_model(config: dict[str, Any]) -> str | None:
    for key in ("base_model_name_or_path", "base_model_name", "base_model"):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _adapter_rank(config: dict[str, Any]) -> int | None:
    for key in ("r", "rank", "lora_rank"):
        value = config.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _adapter_target_modules_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(item, str) and bool(item.strip()) for item in value)
    return False


def _run_lora_adapter_smoke(
    *,
    storage_root: Path,
    router_url: str,
    run_id: str,
    requests_path: Path,
    request_timeout: int,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preflight = preflight or _run_adapter_preflight(storage_root=storage_root)
    if preflight.get("state") == "blocked":
        return {
            "ok": True,
            "state": "blocked",
            "blocked": True,
            "message": "validation adapter payload is not present",
            "preflight": preflight,
            "findings": preflight.get("findings", []),
        }
    if not preflight.get("ok"):
        return {
            "ok": False,
            "state": "invalid",
            "preflight": preflight,
            "findings": preflight.get("findings", []),
        }

    models_payload = _get_json(
        f"{router_url.rstrip('/')}/v1/models?lane=expert",
        timeout=min(max(request_timeout, 20), 120),
    )
    model_ids = _extract_model_ids(models_payload)
    base_record = _chat_prompt_record(
        prompt={
            "id": "lora-adapter-smoke-base",
            "lane": "expert",
            "prompt": "Return the word ok.",
            "max_tokens": 4,
        },
        router_url=router_url,
        run_id=run_id,
        worker_id=0,
        request_timeout=request_timeout,
        suite_label="lora-adapter-smoke",
    )
    adapter_record = _chat_prompt_record(
        prompt={
            "id": "lora-adapter-smoke-adapter",
            "lane": "expert",
            "prompt": "Return the word ok.",
            "max_tokens": 4,
        },
        router_url=router_url,
        run_id=run_id,
        worker_id=1,
        request_timeout=request_timeout,
        suite_label="lora-adapter-smoke",
        model_override=ADAPTER_VALIDATION_MODEL_NAME,
        expected_model=ADAPTER_VALIDATION_MODEL_NAME,
    )
    _append_jsonl(requests_path, base_record)
    _append_jsonl(requests_path, adapter_record)
    checks = {
        "preflight_ready": preflight.get("state") == "ready",
        "models_endpoint_ok": bool(models_payload.get("ok")),
        "adapter_model_listed": ADAPTER_VALIDATION_MODEL_NAME in model_ids,
        "base_request_ok": bool(base_record.get("ok")),
        "adapter_request_ok": bool(adapter_record.get("ok")),
        "model_ids_distinct": base_record.get("model_id") != adapter_record.get("model_id"),
    }
    findings = [
        _finding("error", "LORA_ADAPTER_SMOKE", key)
        for key, value in checks.items()
        if not value
    ]
    return {
        "ok": all(checks.values()),
        "adapter_model": ADAPTER_VALIDATION_MODEL_NAME,
        "base_model": _expected_chat_model("expert"),
        "checks": checks,
        "model_ids": sorted(model_ids),
        "models_endpoint": models_payload,
        "preflight": preflight,
        "requests": [base_record, adapter_record],
        "findings": findings,
    }


def _run_recovery_smoke(
    *,
    router_url: str,
    run_id: str,
    requests_path: Path,
    request_timeout: int,
    host_aliases: dict[str, Any],
) -> dict[str, Any]:
    prompts = [
        {
            "id": "recovery-smoke-coordinator",
            "lane": "coordinator",
            "prompt": (
                "Return one concise sentence confirming ai_fabric.coordinator_model "
                "model and revision facts are available for recovery."
            ),
            "min_retrieval_hits": 1,
            "min_symbolic_facts": 1,
        },
        {
            "id": "recovery-smoke-expert",
            "lane": "expert",
            "prompt": (
                "Return one concise sentence confirming ai_fabric.expert_model "
                "model and revision facts are available for k1s code expert recovery."
            ),
            "min_retrieval_hits": 1,
            "min_symbolic_facts": 1,
        },
    ]
    findings: list[dict[str, str]] = []
    results = []
    for prompt in prompts:
        record = _advisory_prompt_record(
            prompt=prompt,
            router_url=router_url,
            run_id=run_id,
            request_timeout=request_timeout,
            suite_label="recovery-smoke",
        )
        _append_jsonl(requests_path, record)
        results.append(record)
        if not record.get("ok"):
            findings.append(
                _finding(
                    "error",
                    "RECOVERY_PROMPT_FAILED",
                    str(record.get("id") or "unknown prompt"),
                )
            )
    checks = {
        "host_aliases_ok": bool(host_aliases.get("ok")),
        "coordinator_ok": any(
            item.get("expected_lane") == "coordinator" and item.get("ok") for item in results
        ),
        "expert_ok": any(
            item.get("expected_lane") == "expert" and item.get("ok") for item in results
        ),
        "traces_persisted": all(
            bool(item.get("trace_id")) and bool(item.get("trace_path")) for item in results
        ),
    }
    for key, value in checks.items():
        if not value:
            findings.append(_finding("error", "RECOVERY_SMOKE", key))
    return {
        "ok": all(checks.values()),
        "prompt_count": len(prompts),
        "host_aliases": host_aliases,
        "checks": checks,
        "results": results,
        "findings": findings,
    }


def _run_lora_plumbing(
    *,
    root: Path,
    storage_root: Path,
    track: str,
    router_url: str,
    run_id: str,
    requests_path: Path,
    request_timeout: int,
) -> dict[str, Any]:
    tracks = _load_json(root / "model-tracks.json").get("tracks")
    track_config = tracks.get(track) if isinstance(tracks, dict) else None
    findings: list[dict[str, str]] = []
    if not isinstance(track_config, dict):
        return {
            "ok": False,
            "findings": [_finding("error", "LORA_TRACK", f"missing track {track}")],
        }
    adapter_dir = storage_root / "adapters" / "expert"
    record = _chat_prompt_record(
        prompt={
            "id": "lora-plumbing-expert-smoke",
            "lane": "expert",
            "prompt": "Return the word ok.",
            "max_tokens": 4,
        },
        router_url=router_url,
        run_id=run_id,
        worker_id=0,
        request_timeout=request_timeout,
    )
    record["suite"] = "lora-plumbing"
    _append_jsonl(requests_path, record)
    checks = {
        "coordinator_lora_disabled": track_config["coordinator"].get("enable_lora") is False,
        "expert_lora_enabled": track_config["expert"].get("enable_lora") is True,
        "adapter_dir_present": adapter_dir.is_dir(),
        "expert_request_ok": bool(record.get("ok")),
        "expert_model_alias": record.get("model_id") == "k1s-code-expert",
    }
    for key, value in checks.items():
        if not value:
            findings.append(_finding("error", "LORA_PLUMBING", key))
    return {
        "ok": all(checks.values()),
        "track": track,
        "adapter_dir": str(adapter_dir),
        "checks": checks,
        "request": record,
        "findings": findings,
    }


def _run_evidence_closeout(*, das_url: str, f5_evidence_path: Path) -> dict[str, Any]:
    payload = _get_json(f"{das_url.rstrip('/')}/v1/f5/evidence?limit=30", timeout=20)
    f5_evidence_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    kinds = sorted({str(item.get("kind")) for item in records if isinstance(item, dict)})
    required = {"das_cell_bundle", "das_query_trace", "cognitive_signal"}
    missing = sorted(required - set(kinds))
    findings = [
        _finding("error", "F5_EVIDENCE_MISSING", ",".join(missing))
    ] if missing else []
    return {
        "ok": bool(payload.get("ok")) and not missing,
        "record_count": len(records),
        "kinds": kinds,
        "missing": missing,
        "path": str(f5_evidence_path),
        "findings": findings,
    }


def _run_advisor_scenarios(
    *,
    scenarios: list[dict[str, Any]],
    das_url: str,
    router_url: str,
    retrieval_url: str,
    run_id: str,
    output_path: Path,
    request_timeout: int,
) -> dict[str, Any]:
    results = [
        _advisor_scenario_record(
            scenario=scenario,
            das_url=das_url,
            run_id=run_id,
            request_timeout=request_timeout,
            kind="synthetic",
        )
        for scenario in scenarios
    ]
    live_scenario = _live_advisor_scenario(
        router_url=router_url,
        das_url=das_url,
        retrieval_url=retrieval_url,
    )
    results.append(
        _advisor_scenario_record(
            scenario=live_scenario,
            das_url=das_url,
            run_id=run_id,
            request_timeout=request_timeout,
            kind="live",
        )
    )
    findings = [
        _finding(
            "error",
            "ADVISOR_SCENARIO_FAILED",
            str(item.get("id") or "unknown scenario"),
        )
        for item in results
        if not item.get("ok")
    ]
    payload = {
        "ok": not findings,
        "api_version": ADVISOR_SCENARIO_EVAL_API_VERSION,
        "run_id": run_id,
        "scenario_count": len(results),
        "synthetic_count": len(scenarios),
        "live_count": 1,
        "results": results,
        "findings": findings,
        "recorded_at": _utc_now(),
    }
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    payload["path"] = str(output_path)
    return payload


def _live_advisor_scenario(
    *,
    router_url: str,
    das_url: str,
    retrieval_url: str,
) -> dict[str, Any]:
    return {
        "id": "live-runtime-snapshot",
        "description": "Current read-only runtime health, model, DAS, and corpus snapshot.",
        "subject": "ai_fabric.service.ai-router",
        "intent": "validate_runtime_snapshot",
        "query": "Validate the current live AI fabric advisor runtime snapshot.",
        "facts": _runtime_state_facts(
            router_url=router_url,
            das_url=das_url,
            retrieval_url=retrieval_url,
            workerbee_status=None,
        ),
        "expect": {
            "status": "review",
            "min_evidence_refs": 3,
            "risks_absent": [
                "symbolic_blocked_condition",
                "validation_artifact_unhealthy",
            ],
            "blocked_condition_count": 0,
            "authoritative": False,
        },
    }


def _advisor_scenario_record(
    *,
    scenario: dict[str, Any],
    das_url: str,
    run_id: str,
    request_timeout: int,
    kind: str,
) -> dict[str, Any]:
    request_payload = {
        "subject": str(scenario["subject"]),
        "intent": str(scenario["intent"]),
        "query": str(scenario["query"]),
        "facts": scenario.get("facts") if isinstance(scenario.get("facts"), list) else [],
        "limit": int(scenario.get("limit") or 50),
        "request_id": f"{run_id}:{scenario['id']}",
        "use_stored_facts": False,
    }
    started = time.monotonic()
    response = _post_json(
        f"{das_url.rstrip('/')}/v1/advisory/decision",
        request_payload,
        timeout=request_timeout,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    data = response.get("json") if isinstance(response.get("json"), dict) else {}
    decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    checks = _advisor_scenario_checks(
        response=response,
        response_payload=data,
        decision=decision,
        expect=scenario["expect"],
    )
    blocked_conditions = (
        decision.get("blocked_conditions")
        if isinstance(decision.get("blocked_conditions"), list)
        else []
    )
    risks = decision.get("risks") if isinstance(decision.get("risks"), list) else []
    evidence_refs = (
        decision.get("evidence_refs") if isinstance(decision.get("evidence_refs"), list) else []
    )
    return {
        "id": str(scenario["id"]),
        "kind": kind,
        "description": str(scenario.get("description") or ""),
        "subject": request_payload["subject"],
        "intent": request_payload["intent"],
        "status": response.get("status"),
        "elapsed_ms": elapsed_ms,
        "decision_status": decision.get("status"),
        "decision_id": decision.get("decision_id"),
        "authoritative": decision.get("authoritative"),
        "risk_count": len(risks),
        "risks": risks,
        "blocked_condition_count": len(blocked_conditions),
        "blocked_conditions": blocked_conditions,
        "evidence_ref_count": len(evidence_refs),
        "fact_count": len(request_payload["facts"]),
        "checks": checks,
        "ok": all(checks.values()),
        "error": response.get("error"),
        "recorded_at": _utc_now(),
    }


def _advisor_scenario_checks(
    *,
    response: dict[str, Any],
    response_payload: dict[str, Any],
    decision: dict[str, Any],
    expect: dict[str, Any],
) -> dict[str, bool]:
    risks = decision.get("risks") if isinstance(decision.get("risks"), list) else []
    blocked_conditions = (
        decision.get("blocked_conditions")
        if isinstance(decision.get("blocked_conditions"), list)
        else []
    )
    evidence_refs = (
        decision.get("evidence_refs") if isinstance(decision.get("evidence_refs"), list) else []
    )
    expected_blocked_count = expect.get("blocked_condition_count")
    expected_authoritative = expect.get("authoritative", False)
    return {
        "http_ok": response.get("status") == 200,
        "response_ok": bool(response_payload.get("ok")),
        "decision_present": bool(decision),
        "api_version": decision.get("api_version") == ADVISORY_DECISION_API_VERSION,
        "status": decision.get("status") == expect.get("status"),
        "authoritative": decision.get("authoritative") is expected_authoritative,
        "controller_authority": decision.get("controller_authority") == "k1s",
        "min_evidence_refs": len(evidence_refs) >= int(expect.get("min_evidence_refs") or 0),
        "risks_present": all(item in risks for item in expect.get("risks_present", [])),
        "risks_absent": not any(item in risks for item in expect.get("risks_absent", [])),
        "blocked_condition_count": (
            expected_blocked_count is None
            or len(blocked_conditions) == int(expected_blocked_count)
        ),
    }


def _post_json(url: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(  # noqa: S310 - lab validation targets local user-provided URLs.
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            return {"ok": True, "status": response.status, "json": json.loads(raw)}
    except HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "json": {},
            "error": exc.read().decode("utf-8", errors="replace"),
        }
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "status": None, "json": {}, "error": str(exc)}


def _get_json(url: str, *, timeout: int) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid_json"}
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "url": url}


def _is_local_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.hostname in {"127.0.0.1", "::1", "localhost"}


def _extract_model_ids(payload: Any) -> set[str]:
    model_ids: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            model_id = value.get("id")
            if isinstance(model_id, str) and model_id.strip():
                model_ids.add(model_id.strip())
            for key in ("data", "models", "lanes"):
                visit(value.get(key))
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return model_ids


def _gpu_sample() -> dict[str, Any]:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.used,memory.free",
            "--format=csv,noheader",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    sample: dict[str, Any] = {
        "sampled_at": _utc_now(),
        "epoch_seconds": time.time(),
        "ok": result.returncode == 0,
    }
    if result.returncode != 0:
        sample["error"] = result.stderr.strip() or result.stdout.strip()
        return sample
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    parts = [item.strip() for item in line.split(",")]
    sample["raw"] = line
    if len(parts) >= 3:
        sample["gpu_name"] = parts[0]
        sample["memory_used_mib"] = _parse_mib(parts[1])
        sample["memory_free_mib"] = _parse_mib(parts[2])
    return sample


def _parse_mib(value: str) -> int | None:
    token = value.strip().split()[0] if value.strip() else ""
    try:
        return int(token)
    except ValueError:
        return None


def _final_vram_growth_mib(
    samples: list[dict[str, Any]],
    *,
    window_seconds: int,
) -> int | None:
    usable = [
        item
        for item in samples
        if isinstance(item.get("memory_used_mib"), int)
        and isinstance(item.get("epoch_seconds"), float)
    ]
    if not usable:
        return None
    last_epoch = float(usable[-1]["epoch_seconds"])
    window = [
        item
        for item in usable
        if float(item["epoch_seconds"]) >= last_epoch - window_seconds
    ]
    if len(window) < 2:
        window = usable
    return int(window[-1]["memory_used_mib"]) - int(window[0]["memory_used_mib"])


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _runtime_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _iter_corpus_files(source_root: Path, source_name: str):
    seen: set[Path] = set()
    for relative_name in CORPUS_SOURCE_PATHS[source_name]:
        base = source_root / relative_name
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() not in CORPUS_SUFFIXES:
                continue
            relative = candidate.relative_to(source_root)
            if any(part in CORPUS_IGNORE_DIRS for part in relative.parts):
                continue
            if relative in seen:
                continue
            seen.add(relative)
            yield candidate


def _runtime_facts(
    *,
    project: str,
    track: str,
    config: dict[str, Any],
    k1s_root: Path,
    stage_dir: Path,
) -> list[dict[str, Any]]:
    facts = [
        _runtime_fact(
            "ai_fabric.relationship_vocabulary",
            "api_version",
            RUNTIME_FACT_SOURCE,
            source=RUNTIME_FACT_SOURCE,
        ),
        *[
            _runtime_fact(
                "ai_fabric.relationship_vocabulary",
                "predicate",
                predicate,
                source=RUNTIME_FACT_SOURCE,
            )
            for predicate in RUNTIME_RELATIONSHIP_PREDICATES
        ],
        _runtime_fact("ai_fabric.track", "configured_as", track, source=RUNTIME_FACT_SOURCE),
        _runtime_fact(
            "ai_fabric.advisory_decision",
            "api_version",
            ADVISORY_DECISION_API_VERSION,
            source=RUNTIME_FACT_SOURCE,
        ),
        _runtime_fact(
            "ai_fabric.coordinator_model",
            "model",
            config["coordinator"]["model"],
            source=RUNTIME_FACT_SOURCE,
        ),
        _runtime_fact(
            "ai_fabric.coordinator_model",
            "revision",
            config["coordinator"]["revision"],
            source=RUNTIME_FACT_SOURCE,
        ),
        _runtime_fact(
            "ai_fabric.expert_model",
            "model",
            config["expert"]["model"],
            source=RUNTIME_FACT_SOURCE,
        ),
        _runtime_fact(
            "ai_fabric.expert_model",
            "revision",
            config["expert"]["revision"],
            source=RUNTIME_FACT_SOURCE,
        ),
        _runtime_fact("repo.workerbee", "commit", _git_rev(REPO_ROOT)),
    ]
    if project:
        facts.append(_runtime_fact("workerbee.project", "name", project))
    k1s_rev = _git_rev(k1s_root.expanduser().resolve())
    if k1s_rev:
        facts.append(_runtime_fact("repo.k1s", "commit", k1s_rev))
    facts.extend(
        _stage_runtime_facts(
            stage_dir=stage_dir,
            project=project,
            track=track,
            config=config,
        )
    )
    return facts


def _stage_runtime_facts(
    *,
    stage_dir: Path,
    project: str,
    track: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    service_subjects: dict[str, str] = {}
    for doc in _stage_deployments(stage_dir):
        metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        name = str(metadata.get("name") or "")
        if not name:
            continue
        namespace = str(metadata.get("namespace") or "default")
        subject = f"ai_fabric.service.{name}"
        service_subjects[name] = subject
        facts.extend(
            [
                _runtime_fact("ai_fabric.lab", "owns_service", subject, source=RUNTIME_FACT_SOURCE),
                _runtime_fact(subject, "name", name, source=RUNTIME_FACT_SOURCE),
                _runtime_fact(subject, "namespace", namespace, source=RUNTIME_FACT_SOURCE),
                _runtime_fact(subject, "kind", "Deployment", source=RUNTIME_FACT_SOURCE),
                _runtime_fact(subject, "image", spec.get("image"), source=RUNTIME_FACT_SOURCE),
                _runtime_fact(subject, "track", track, source=RUNTIME_FACT_SOURCE),
            ]
        )
        if project:
            facts.append(
                _runtime_fact(
                    "workerbee.project",
                    "owns_service",
                    subject,
                    source=RUNTIME_FACT_SOURCE,
                )
            )
        service = spec.get("service") if isinstance(spec.get("service"), dict) else {}
        if service:
            facts.extend(_service_port_facts(subject, service))
        resources = spec.get("resources") if isinstance(spec.get("resources"), dict) else {}
        facts.extend(_resource_facts(subject, resources))
        for storage_resource in AI_FABRIC_SERVICE_STORAGE_RESOURCES.get(name, ()):
            facts.append(
                _runtime_fact(
                    subject,
                    "requires_resource",
                    {"kind": "storage", "path": storage_resource},
                    source=RUNTIME_FACT_SOURCE,
                )
            )
        for advisory_capability in AI_FABRIC_SERVICE_ADVISORY_SUPPORT.get(name, ()):
            facts.append(
                _runtime_fact(
                    subject,
                    "supports_advisory",
                    advisory_capability,
                    source=RUNTIME_FACT_SOURCE,
                )
            )

    for name, dependencies in AI_FABRIC_SERVICE_DEPENDENCIES.items():
        subject = service_subjects.get(name)
        if not subject:
            continue
        for dependency in dependencies:
            dependency_subject = service_subjects.get(dependency)
            if dependency_subject:
                facts.append(
                    _runtime_fact(
                        subject,
                        "depends_on",
                        dependency_subject,
                        source=RUNTIME_FACT_SOURCE,
                    )
                )

    facts.extend(_model_runtime_facts(config=config, service_subjects=service_subjects))
    facts.extend(_validation_artifact_facts())
    return facts


def _stage_deployments(stage_dir: Path) -> list[dict[str, Any]]:
    manifests_dir = stage_dir / "manifests"
    deployments: list[dict[str, Any]] = []
    for path in sorted(manifests_dir.glob("*.yaml")):
        for doc in _load_yaml_documents(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") == "Deployment":
                deployments.append(doc)
    return deployments


def _service_port_facts(subject: str, service: dict[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    service_port = service.get("port")
    target_port = service.get("targetPort")
    if service_port is not None:
        facts.append(
            _runtime_fact(subject, "service_port", service_port, source=RUNTIME_FACT_SOURCE)
        )
        try:
            port_value = int(service_port)
        except (TypeError, ValueError):
            port_value = 0
        if port_value in {18180, 18181, 18182}:
            facts.append(
                _runtime_fact(
                    subject,
                    "host_alias",
                    f"http://127.0.0.1:{port_value}",
                    source=RUNTIME_FACT_SOURCE,
                )
            )
    if target_port is not None:
        facts.append(
            _runtime_fact(subject, "target_port", target_port, source=RUNTIME_FACT_SOURCE)
        )
    return facts


def _resource_facts(subject: str, resources: dict[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for scope in ("requests", "limits"):
        values = resources.get(scope)
        if not isinstance(values, dict):
            continue
        for resource_name, value in sorted(values.items(), key=lambda item: str(item[0])):
            facts.append(
                _runtime_fact(
                    subject,
                    "requires_resource",
                    {
                        "kind": "compute",
                        "scope": scope,
                        "name": str(resource_name),
                        "value": value,
                    },
                    source=RUNTIME_FACT_SOURCE,
                )
            )
    return facts


def _model_runtime_facts(
    *,
    config: dict[str, Any],
    service_subjects: dict[str, str],
) -> list[dict[str, Any]]:
    lane_services = {
        "coordinator": "ai-coordinator",
        "expert": "ai-expert",
    }
    facts: list[dict[str, Any]] = []
    for lane, service_name in lane_services.items():
        lane_config = config.get(lane)
        if not isinstance(lane_config, dict):
            continue
        model_subject = f"ai_fabric.model.{lane}"
        service_subject = service_subjects.get(service_name)
        facts.extend(
            [
                _runtime_fact(model_subject, "lane", lane, source=RUNTIME_FACT_SOURCE),
                _runtime_fact(
                    model_subject,
                    "model",
                    lane_config.get("model"),
                    source=RUNTIME_FACT_SOURCE,
                ),
                _runtime_fact(
                    model_subject,
                    "revision",
                    lane_config.get("revision"),
                    source=RUNTIME_FACT_SOURCE,
                ),
                _runtime_fact(
                    model_subject,
                    "served_model_name",
                    lane_config.get("served_model_name"),
                    source=RUNTIME_FACT_SOURCE,
                ),
                _runtime_fact(
                    model_subject,
                    "max_model_len",
                    lane_config.get("max_model_len"),
                    source=RUNTIME_FACT_SOURCE,
                ),
            ]
        )
        if service_subject:
            facts.append(
                _runtime_fact(
                    service_subject,
                    "serves_model",
                    model_subject,
                    source=RUNTIME_FACT_SOURCE,
                )
            )
        lora_modules = lane_config.get("lora_modules")
        if isinstance(lora_modules, list):
            for module in lora_modules:
                if isinstance(module, dict):
                    facts.extend(
                        _lora_module_facts(
                            lane=lane,
                            module=module,
                            model_subject=model_subject,
                            service_subject=service_subject,
                        )
                    )
    return facts


def _lora_module_facts(
    *,
    lane: str,
    module: dict[str, Any],
    model_subject: str,
    service_subject: str | None,
) -> list[dict[str, Any]]:
    name = str(module.get("name") or "")
    if not name:
        return []
    adapter_subject = f"ai_fabric.adapter.{name}"
    facts = [
        _runtime_fact(adapter_subject, "lane", lane, source=RUNTIME_FACT_SOURCE),
        _runtime_fact(adapter_subject, "name", name, source=RUNTIME_FACT_SOURCE),
        _runtime_fact(
            adapter_subject,
            "path",
            module.get("path"),
            source=RUNTIME_FACT_SOURCE,
        ),
        _runtime_fact(
            adapter_subject,
            "base_model",
            module.get("base_model_name"),
            source=RUNTIME_FACT_SOURCE,
        ),
        _runtime_fact(
            adapter_subject,
            "depends_on",
            model_subject,
            source=RUNTIME_FACT_SOURCE,
        ),
        _runtime_fact(
            adapter_subject,
            "requires_resource",
            {"kind": "adapter_path", "path": module.get("path")},
            source=RUNTIME_FACT_SOURCE,
        ),
    ]
    if service_subject:
        facts.append(
            _runtime_fact(
                service_subject,
                "serves_model",
                adapter_subject,
                source=RUNTIME_FACT_SOURCE,
            )
        )
    return facts


def _validation_artifact_facts() -> list[dict[str, Any]]:
    return [
        _runtime_fact(
            "ai_fabric.runtime_validation",
            "produced_artifact",
            f"runs/<run-id>/{filename}",
            source=RUNTIME_FACT_SOURCE,
        )
        for filename in RUNTIME_OUTPUT_FILES
    ]


def _runtime_state_facts(
    *,
    router_url: str,
    das_url: str,
    retrieval_url: str,
    workerbee_status: Path | None,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = [
        _runtime_fact(
            "ai_fabric.advisory_decision",
            "api_version",
            ADVISORY_DECISION_API_VERSION,
            source=RUNTIME_FACT_SOURCE,
        )
    ]
    health_targets = {
        "ai-router": router_url,
        "das-bridge": das_url,
        "retrieval-indexer": retrieval_url,
    }
    for service_name, base_url in health_targets.items():
        subject = f"ai_fabric.service.{service_name}"
        health = _local_health(base_url)
        readiness = "ready" if health.get("ok") is True else "unavailable"
        if health.get("skipped"):
            readiness = "unknown"
        facts.extend(
            [
                _runtime_fact(subject, "readiness", readiness, source=RUNTIME_FACT_SOURCE),
                _runtime_fact(subject, "host_alias_health", health, source=RUNTIME_FACT_SOURCE),
            ]
        )
        if service_name == "das-bridge" and isinstance(health.get("payload"), dict):
            payload = health["payload"]
            for key in ("fact_count", "f5_evidence_count"):
                if key in payload:
                    facts.append(
                        _runtime_fact(
                            subject,
                            key,
                            payload[key],
                            source=RUNTIME_FACT_SOURCE,
                        )
                    )
        if service_name == "retrieval-indexer" and isinstance(health.get("payload"), dict):
            payload = health["payload"]
            for key in ("document_count", "chunk_count", "qdrant_indexed"):
                if key in payload:
                    facts.append(
                        _runtime_fact(
                            "ai_fabric.retrieval_corpus",
                            key,
                            payload[key],
                            source=RUNTIME_FACT_SOURCE,
                        )
                    )
    facts.extend(_model_lane_state_facts(router_url=router_url))
    if workerbee_status is not None:
        facts.extend(_workerbee_status_facts(workerbee_status))
    return facts


def _local_health(base_url: str) -> dict[str, Any]:
    if not _is_local_url(base_url):
        return {
            "ok": None,
            "skipped": True,
            "url": f"{base_url.rstrip('/')}/healthz",
            "reason": "non_local_url",
        }
    url = f"{base_url.rstrip('/')}/healthz"
    payload = _get_json(url, timeout=5)
    return {
        "ok": bool(payload.get("ok")),
        "url": url,
        "service": payload.get("service"),
        "error": payload.get("error"),
        "payload": payload,
    }


def _model_lane_state_facts(*, router_url: str) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    if not _is_local_url(router_url):
        return [
            _runtime_fact(
                f"ai_fabric.model.{lane}",
                "model_lane_readiness",
                {"ok": None, "lane": lane, "skipped": True, "reason": "non_local_router_url"},
                source=RUNTIME_FACT_SOURCE,
            )
            for lane in ("coordinator", "expert")
        ]
    for lane in ("coordinator", "expert"):
        payload = _get_json(f"{router_url.rstrip('/')}/v1/models?lane={lane}", timeout=15)
        models = payload.get("data") if isinstance(payload.get("data"), list) else []
        errors = payload.get("errors") if isinstance(payload.get("errors"), dict) else {}
        facts.append(
            _runtime_fact(
                f"ai_fabric.model.{lane}",
                "model_lane_readiness",
                {
                    "ok": bool(payload.get("ok")),
                    "lane": lane,
                    "model_count": len(models),
                    "errors": errors,
                    "url": f"{router_url.rstrip('/')}/v1/models?lane={lane}",
                },
                source=RUNTIME_FACT_SOURCE,
            )
        )
    return facts


def _workerbee_status_facts(path: Path) -> list[dict[str, Any]]:
    if not path.expanduser().is_file():
        return [
            _runtime_fact(
                "workerbee.project",
                "status_snapshot",
                {"ok": False, "path": str(path), "error": "missing"},
                source=RUNTIME_FACT_SOURCE,
            )
        ]
    try:
        payload = _load_json(path.expanduser())
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        return [
            _runtime_fact(
                "workerbee.project",
                "status_snapshot",
                {"ok": False, "path": str(path), "error": str(exc)},
                source=RUNTIME_FACT_SOURCE,
            )
        ]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    latest = (
        data.get("latest_deployment")
        if isinstance(data.get("latest_deployment"), dict)
        else {}
    )
    app_status = data.get("app_status") if isinstance(data.get("app_status"), dict) else {}
    if not app_status and isinstance(latest.get("app_status"), dict):
        app_status = latest["app_status"]
    facts = [
        _runtime_fact(
            "workerbee.project",
            "status_snapshot",
            {
                "ok": bool(payload.get("ok", True)),
                "path": str(path.expanduser()),
                "deployment_id": app_status.get("deployment_id") or latest.get("id"),
            },
            source=RUNTIME_FACT_SOURCE,
        )
    ]
    if app_status:
        facts.extend(
            [
                _runtime_fact(
                    "workerbee.project",
                    "readiness",
                    "ready" if app_status.get("ready") else "degraded",
                    source=RUNTIME_FACT_SOURCE,
                ),
                _runtime_fact(
                    "workerbee.project",
                    "degraded_workload_count",
                    app_status.get("degraded_workload_count", 0),
                    source=RUNTIME_FACT_SOURCE,
                ),
            ]
        )
        for item in app_status.get("ready_workloads") or []:
            if isinstance(item, dict) and item.get("name"):
                facts.append(
                    _runtime_fact(
                        f"ai_fabric.service.{item['name']}",
                        "readiness",
                        "ready",
                        source=RUNTIME_FACT_SOURCE,
                    )
                )
        for item in app_status.get("degraded_workloads") or []:
            if isinstance(item, dict) and item.get("name"):
                facts.append(
                    _runtime_fact(
                        f"ai_fabric.service.{item['name']}",
                        "readiness",
                        "degraded",
                        source=RUNTIME_FACT_SOURCE,
                    )
                )
    return facts


def _phase_report_facts(report: dict[str, Any]) -> list[dict[str, Any]]:
    source = "k1s.fabric.phase-assurance/v1"
    if report.get("api_version") != source:
        raise ValueError("unexpected phase report api_version")
    phases = report.get("phases")
    if not isinstance(phases, dict):
        raise ValueError("phase report must include phases object")
    phase_order = report.get("phase_order")
    if not isinstance(phase_order, list):
        phase_order = sorted(phases)

    facts = [
        _runtime_fact("k1s.fabric.phase_report", "api_version", source, source=source),
        _runtime_fact(
            "k1s.fabric.phase_report",
            "kind",
            str(report.get("kind") or ""),
            source=source,
        ),
    ]
    ready_phases = report.get("ready_phases")
    if isinstance(ready_phases, list):
        for phase_id in ready_phases:
            if isinstance(phase_id, str):
                facts.append(
                    _runtime_fact(
                        "k1s.fabric.phase_report",
                        "ready_phase",
                        phase_id,
                        source=source,
                    )
                )

    for phase_id in phase_order:
        if not isinstance(phase_id, str):
            continue
        phase = phases.get(phase_id)
        if not isinstance(phase, dict):
            continue
        subject = f"k1s.fabric.phase.{phase_id}"
        facts.append(_runtime_fact(subject, "status", phase.get("status"), source=source))
        gate = phase.get("gate") if isinstance(phase.get("gate"), dict) else {}
        facts.append(
            _runtime_fact(subject, "gate_ready", bool(gate.get("ready")), source=source)
        )
        blocked_by = gate.get("blocked_by") if isinstance(gate, dict) else []
        if isinstance(blocked_by, list):
            for blocker in blocked_by:
                if isinstance(blocker, str):
                    facts.append(_runtime_fact(subject, "blocked_by", blocker, source=source))
        facts.extend(
            _phase_evidence_facts(
                phase_id,
                phase.get("present"),
                present=True,
                source=source,
            )
        )
        facts.extend(
            _phase_evidence_facts(
                phase_id,
                phase.get("missing"),
                present=False,
                source=source,
            )
        )
        facts.extend(_phase_evidence_value_facts(phase_id, phase.get("evidence"), source=source))
    return facts


def _phase_evidence_value_facts(
    phase_id: str,
    evidence: Any,
    *,
    source: str,
) -> list[dict[str, Any]]:
    if not isinstance(evidence, dict):
        return []
    facts: list[dict[str, Any]] = []
    for key, value in sorted(evidence.items(), key=lambda item: str(item[0])):
        if not isinstance(key, str):
            continue
        subject = f"k1s.fabric.phase.{phase_id}.evidence.{key}"
        facts.append(_runtime_fact(subject, "value", value, source=source))
        if isinstance(value, dict):
            for detail_key, detail_value in sorted(value.items(), key=lambda item: str(item[0])):
                if isinstance(detail_key, str):
                    facts.append(
                        _runtime_fact(
                            subject,
                            f"detail.{detail_key}",
                            detail_value,
                            source=source,
                        )
                    )
    return facts


def _phase_evidence_facts(
    phase_id: str,
    values: Any,
    *,
    present: bool,
    source: str,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    facts = []
    for value in values:
        if isinstance(value, str):
            facts.append(
                _runtime_fact(
                    f"k1s.fabric.phase.{phase_id}.evidence.{value}",
                    "present",
                    present,
                    source=source,
                )
            )
    return facts


def _f5_evidence_payload(
    *,
    storage_root: Path,
    site_id: str,
    peer_site_id: str,
    project: str,
    track: str,
    query_id: str,
) -> dict[str, Any]:
    now = _utc_now()
    bundle_id = _stable_id("das-bundle", [site_id, project or "workerbee", track])
    trace_id = _stable_id("das-query", [bundle_id, query_id, track])
    replication_id = _stable_id("das-replication", [bundle_id, site_id, peer_site_id])
    signal_id = _stable_id("cognitive-signal", [bundle_id, trace_id])
    facts_ref = f"das://{site_id}/runtime/facts.jsonl"
    records = {
        "das_cell_bundles": [
            {
                "bundle_id": bundle_id,
                "site_id": site_id,
                "cell_id": "runtime",
                "version": now[:10],
                "storage_ref": str(storage_root / "das"),
                "facts_ref": facts_ref,
                "status": "ready",
                "labels": {"project": project, "track": track, "workerbee_lab": "ai-fabric"},
                "created_at": now,
                "updated_at": now,
            }
        ],
        "das_query_traces": [
            {
                "trace_id": trace_id,
                "bundle_id": bundle_id,
                "site_id": site_id,
                "query_id": query_id,
                "query_kind": "advisory",
                "local_first": True,
                "warmed_refs": [facts_ref],
                "promoted_refs": [f"qdrant://ai_fabric_corpus/{track or 'default'}"],
                "fallback_sites": [],
                "result_ref": f"workerbee://runs/f5/{trace_id}",
                "created_at": now,
            }
        ],
        "das_replications": [
            {
                "replication_id": replication_id,
                "bundle_id": bundle_id,
                "source_site_id": site_id,
                "target_site_id": peer_site_id,
                "mode": "controlled",
                "status": "planned",
                "approved_by": "operator",
                "reason": "warm peer DAS cell without making WAN a hot query path",
                "created_at": now,
                "updated_at": now,
            }
        ],
        "cognitive_signals": [
            {
                "signal_id": signal_id,
                "subject_type": "das-cell",
                "subject_id": bundle_id,
                "signal_kind": "continuity",
                "continuity_ref": f"workerbee://runs/f5/{trace_id}",
                "coherence_score": 1.0,
                "overload_state": "nominal",
                "review_gate": "operator_review",
                "advisory_trace_id": trace_id,
                "created_at": now,
            }
        ],
    }
    return {
        "api_version": "workerbee.ai-fabric.f5-evidence/v1",
        "kind": "AIFabricF5Evidence",
        "generated_at": now,
        "records": records,
    }


def _f5_evidence_facts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    source = "workerbee.ai-fabric.f5-evidence/v1"
    records = payload.get("records") if isinstance(payload.get("records"), dict) else {}
    facts = [
        _runtime_fact("workerbee.ai_fabric.f5_evidence", "api_version", source, source=source)
    ]
    evidence_keys = {
        "das_cell_bundles": "das_cell_bundles",
        "das_query_traces": "local_first_query_warming_promotion",
        "das_replications": "controlled_cross_site_replication",
        "cognitive_signals": "cognitive_fabric_substrate",
    }
    for record_group, evidence_key in evidence_keys.items():
        items = records.get(record_group)
        if not isinstance(items, list):
            continue
        facts.append(
            _runtime_fact(
                f"k1s.fabric.phase.F5.evidence.{evidence_key}",
                "workerbee_record_count",
                len(items),
                source=source,
            )
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            record_id = (
                item.get("bundle_id")
                or item.get("trace_id")
                or item.get("replication_id")
                or item.get("signal_id")
            )
            if record_id:
                facts.append(
                    _runtime_fact(
                        f"k1s.fabric.phase.F5.evidence.{evidence_key}",
                        "workerbee_record",
                        str(record_id),
                        source=source,
                    )
                )
    return facts


def _runtime_fact(
    subject: str,
    predicate: str,
    obj: Any,
    *,
    source: str = "ai_fabric_lab.py",
) -> dict[str, Any]:
    return {
        "namespace": "runtime",
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "source": source,
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _stable_id(prefix: str, parts: list[str]) -> str:
    digest = hashlib.sha256("\n".join(str(part) for part in parts).encode()).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _git_rev(path: Path) -> str | None:
    if not path.exists():
        return None
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _stage_env_value(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    docs = _load_yaml_documents(path.read_text(encoding="utf-8"))
    for doc in docs:
        env = ((doc.get("spec") or {}).get("env") or []) if isinstance(doc, dict) else []
        for item in env:
            if isinstance(item, dict) and item.get("name") == name:
                value = item.get("value")
                return str(value) if value is not None else None
    return None


def _post_das_fact(das_url: str, fact: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(fact).encode("utf-8")
    request = Request(  # noqa: S310 - user-provided lab URL for local DAS import.
        f"{das_url.rstrip('/')}/v1/facts",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _post_runtime_facts(
    das_url: str,
    facts: list[dict[str, Any]],
    findings: list[dict[str, str]],
) -> dict[str, Any]:
    body = json.dumps({"facts": facts}).encode("utf-8")
    request = Request(  # noqa: S310 - user-provided lab URL for local DAS import.
        f"{das_url.rstrip('/')}/v1/import/runtime",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        findings.append(_finding("error", "DAS_RUNTIME_IMPORT_FAILED", str(exc)))
        return {}
    return payload if isinstance(payload, dict) else {}


def _post_facts(
    das_url: str,
    facts: list[dict[str, Any]],
    findings: list[dict[str, str]],
) -> list[dict[str, Any]]:
    posted = []
    for fact in facts:
        try:
            posted.append(_post_das_fact(das_url, fact))
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            findings.append(_finding("error", "DAS_IMPORT_FAILED", str(exc)))
            break
    return posted


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
    findings.extend(_validate_lora_lane(track, lane, config))
    return findings


def _validate_lora_lane(track: str, lane: str, config: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    modules = config.get("lora_modules")
    if modules is None:
        return findings
    if not config.get("enable_lora"):
        findings.append(_finding("error", "MODEL_LORA_ENABLE", f"{track}.{lane}"))
    if not isinstance(modules, list) or not modules:
        findings.append(_finding("error", "MODEL_LORA_MODULES", f"{track}.{lane}"))
        return findings
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            findings.append(_finding("error", "MODEL_LORA_MODULE", f"{track}.{lane}[{index}]"))
            continue
        for key in ("name", "path", "base_model_name"):
            if not isinstance(module.get(key), str) or not str(module.get(key)).strip():
                findings.append(
                    _finding(
                        "error",
                        "MODEL_LORA_MODULE_REQUIRED",
                        f"{track}.{lane}[{index}].{key}",
                    )
                )
        rank = module.get("max_lora_rank", config.get("max_lora_rank"))
        try:
            rank_value = int(rank)
        except (TypeError, ValueError):
            rank_value = 0
        if rank_value <= 0 or rank_value > ADAPTER_MAX_LORA_RANK:
            findings.append(
                _finding("error", "MODEL_LORA_RANK", f"{track}.{lane}[{index}]")
            )
    for key in ("max_loras", "max_lora_rank"):
        if config.get(key) is None:
            continue
        try:
            value = int(config[key])
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            findings.append(_finding("error", "MODEL_LORA_LIMIT", f"{track}.{lane}.{key}"))
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
