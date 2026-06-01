#!/usr/bin/env python3
"""Validate and prepare the AI fabric lab example."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = REPO_ROOT / "examples" / "ai-fabric-lab"
SRC_ROOT = REPO_ROOT / "src"
REVISION_HEX_LEN = 40

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
    runtime_facts.add_argument("--project", default="")
    runtime_facts.add_argument("--track", default="")
    runtime_facts.add_argument("--k1s-root", type=Path, default=REPO_ROOT.parent / "k1s")
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
            project=args.project,
            track=args.track or None,
            k1s_root=args.k1s_root,
            phase_report=args.phase_report,
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
    project: str,
    track: str | None,
    k1s_root: Path,
    phase_report: Path | None = None,
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
    )
    findings: list[dict[str, str]] = []
    if phase_report is not None:
        try:
            facts.extend(_phase_report_facts(_load_json(phase_report)))
        except ValueError as exc:
            findings.append(_finding("error", "PHASE_REPORT_INVALID", str(exc)))
    posted = [] if findings else _post_facts(das_url, facts, findings)
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
) -> list[dict[str, Any]]:
    facts = [
        _runtime_fact("ai_fabric.track", "configured_as", track),
        _runtime_fact("ai_fabric.coordinator_model", "model", config["coordinator"]["model"]),
        _runtime_fact("ai_fabric.coordinator_model", "revision", config["coordinator"]["revision"]),
        _runtime_fact("ai_fabric.expert_model", "model", config["expert"]["model"]),
        _runtime_fact("ai_fabric.expert_model", "revision", config["expert"]["revision"]),
        _runtime_fact("repo.workerbee", "commit", _git_rev(REPO_ROOT)),
    ]
    if project:
        facts.append(_runtime_fact("workerbee.project", "name", project))
    k1s_rev = _git_rev(k1s_root.expanduser().resolve())
    if k1s_rev:
        facts.append(_runtime_fact("repo.k1s", "commit", k1s_rev))
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
