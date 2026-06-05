#!/usr/bin/env python3
"""TrueAGI Hyperon experimental advisory sidecar for the AI fabric lab."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PROVIDER = os.getenv("HYPERON_PROVIDER", "trueagi-hyperon-experimental")
HYPERON_VERSION = os.getenv("HYPERON_VERSION", "0.2.10")
HYPERON_SOURCE_REPO = os.getenv(
    "HYPERON_SOURCE_REPO",
    "https://github.com/trueagi-io/hyperon-experimental",
)
HYPERON_SOURCE_REF = os.getenv("HYPERON_SOURCE_REF", "v0.2.10")
HYPERON_SOURCE_COMMIT = os.getenv(
    "HYPERON_SOURCE_COMMIT",
    "3f76dc460da6961f57f69f6c3e550c59c74ada83",
)
DATA_DIR = Path(os.getenv("HYPERON_ADVISOR_DATA_DIR", "/data/hyperon-advisor"))
F5_EVIDENCE_LOG = DATA_DIR / "f5-evidence.jsonl"
SITE_ID = os.getenv("AI_FABRIC_SITE_ID", "site-a")
CELL_ID = os.getenv("AI_FABRIC_HYPERON_CELL_ID", "trueagi-runtime")
ADVISORY_API_VERSION = "workerbee.ai-fabric.trueagi-hyperon-advisor/v1"
ADVISORY_DECISION_API_VERSION = "workerbee.ai-fabric.advisory-decision/v1"
K1S_ADVISORY_IMPORT_API_VERSION = "workerbee.ai-fabric.k1s-advisory-import/v1"
F5_EVIDENCE_API_VERSION = "workerbee.ai-fabric.f5-evidence/v1"
TOKEN_RE = re.compile(r"[^A-Za-z0-9_]+")
DEGRADED_STATES = {
    "blocked",
    "degraded",
    "down",
    "failed",
    "false",
    "invalid",
    "missing",
    "not_ready",
    "stale",
    "unavailable",
    "unhealthy",
}
LOG_LOCK = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017


def _provider_source() -> dict[str, str]:
    return {
        "repo": HYPERON_SOURCE_REPO,
        "ref": HYPERON_SOURCE_REF,
        "commit": HYPERON_SOURCE_COMMIT,
        "runtime_version": HYPERON_VERSION,
    }


def _stable_id(prefix: str, parts: list[Any]) -> str:
    payload = json.dumps(parts, sort_keys=True, default=str).encode()
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:16]}"


def _fact_id(fact: dict[str, Any]) -> str:
    stable = {
        "namespace": str(fact.get("namespace") or "runtime"),
        "subject": str(fact.get("subject") or ""),
        "predicate": str(fact.get("predicate") or ""),
        "object": fact.get("object"),
        "source": str(fact.get("source") or PROVIDER),
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _normalize_facts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("facts")
    if not isinstance(raw, list):
        return []
    facts: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        predicate = str(item.get("predicate") or "").strip()
        if not subject or not predicate:
            continue
        fact = dict(item)
        fact.setdefault("namespace", "runtime")
        fact.setdefault("source", PROVIDER)
        fact.setdefault("id", _fact_id(fact))
        facts.append(fact)
    return facts


def _metta_symbol(value: Any) -> str:
    symbol = TOKEN_RE.sub("_", str(value).strip()).strip("_")
    if not symbol:
        symbol = "empty"
    if symbol[0].isdigit():
        symbol = f"v_{symbol}"
    return symbol


def _metta_atom(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        value = value.get("state") or value.get("status") or json.dumps(value, sort_keys=True)
    return _metta_symbol(value)


def _run_metta_smoke() -> dict[str, Any]:
    try:
        import importlib.metadata as metadata

        from hyperon import MeTTa

        metta = MeTTa()
        result = metta.run("!(+ 1 2)")
        rendered = str(result)
        return {
            "ok": "3" in rendered,
            "runtime_version": metadata.version("hyperon"),
            "program": "!(+ 1 2)",
            "result": rendered,
        }
    except Exception as exc:  # pragma: no cover - depends on runtime image package.
        return {"ok": False, "error": str(exc)}


def _run_metta_evaluation(facts: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        from hyperon import MeTTa

        metta = MeTTa()
        program: list[str] = []
        probes: list[dict[str, str]] = []
        for fact in facts[:32]:
            predicate = _metta_symbol(fact.get("predicate"))
            subject = _metta_symbol(fact.get("subject"))
            obj = _metta_atom(fact.get("object"))
            expr = f"(= ({predicate} {subject}) {obj})"
            metta.run(expr)
            program.append(expr)
        for fact in facts[:8]:
            predicate = _metta_symbol(fact.get("predicate"))
            subject = _metta_symbol(fact.get("subject"))
            query = f"!({predicate} {subject})"
            probes.append({"query": query, "result": str(metta.run(query))})
        return {"ok": True, "program": program, "probes": probes}
    except Exception as exc:  # pragma: no cover - depends on runtime image package.
        return {"ok": False, "error": str(exc), "program": [], "probes": []}


def _object_state(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        for key in ("state", "status"):
            if value.get(key) is not None:
                return str(value.get(key)).strip().lower()
        return json.dumps(value, sort_keys=True).lower()
    return str(value).strip().lower()


def _advisory_decision(
    payload: dict[str, Any],
    *,
    facts: list[dict[str, Any]],
    metta: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    subject = str(payload.get("subject") or payload.get("subject_id") or "fabric.advisory")
    intent = str(payload.get("intent") or "advise_runtime")
    query = str(payload.get("query") or "")
    evidence_refs = [f"hyperon-fact://{fact['id']}" for fact in facts]
    risks: list[str] = []
    blocked_conditions: list[dict[str, str]] = []

    if not facts:
        risks.append("missing_symbolic_evidence")
    for fact in facts:
        fact_subject = str(fact.get("subject") or "")
        predicate = str(fact.get("predicate") or "")
        state = _object_state(fact.get("object"))
        condition = f"{fact_subject}.{predicate}"
        if predicate == "blocked_by":
            risks.append("fabric_phase_gate_blocked")
            blocked_conditions.append(
                {"condition": condition, "reason": f"blocked_by={fact.get('object')}"}
            )
        elif predicate == "gate_ready" and state == "false":
            risks.append("fabric_phase_gate_blocked")
            blocked_conditions.append({"condition": condition, "reason": "gate_ready=false"})
        elif predicate == "present" and state == "false":
            risks.append("missing_phase_evidence")
            blocked_conditions.append({"condition": condition, "reason": "evidence missing"})
        elif state in DEGRADED_STATES:
            risks.append("symbolic_blocked_condition")
            blocked_conditions.append({"condition": condition, "reason": state})
            if "phase_report" in fact_subject and state == "stale":
                risks.append("phase_report_stale")
            if "adapter" in fact_subject:
                risks.append("lora_adapter_not_ready")
            if "artifact" in predicate or isinstance(fact.get("object"), dict):
                risks.append("validation_artifact_unhealthy")
    if not metta.get("ok"):
        risks.append("metta_runtime_unavailable")

    risks = sorted(set(risks))
    status = "blocked" if blocked_conditions else "review"
    if status == "blocked":
        recommended_action = "diverge until blocked symbolic conditions are cleared"
        confidence = 0.55
    else:
        recommended_action = "retain as advisory evidence and verify live k1s state"
        confidence = 0.72

    return {
        "api_version": ADVISORY_DECISION_API_VERSION,
        "decision_id": _stable_id("trueagi-hyperon-decision", [subject, intent, query, facts]),
        "provider": PROVIDER,
        "provider_source": _provider_source(),
        "subject": subject,
        "intent": intent,
        "query": query,
        "status": status,
        "recommended_action": recommended_action,
        "recommendation": recommended_action,
        "confidence": confidence,
        "evidence_refs": evidence_refs,
        "risks": risks,
        "blocked_conditions": blocked_conditions,
        "authoritative": False,
        "controller_authority": "k1s",
        "facts": facts,
        "metta": metta,
        "created_at": now,
    }


def _f5_evidence(
    *,
    payload: dict[str, Any],
    decision: dict[str, Any],
    facts: list[dict[str, Any]],
    trace_id: str,
    now: str,
) -> list[dict[str, Any]]:
    project = str(payload.get("project") or "workerbee")
    run_id = str(payload.get("run_id") or trace_id)
    bundle_id = _stable_id("trueagi-hyperon-bundle", [SITE_ID, CELL_ID, project])
    query_id = str(payload.get("query_id") or decision.get("decision_id") or trace_id)
    result_ref = f"hyperon://{SITE_ID}/{CELL_ID}/{run_id}/advisory/{trace_id}"
    fact_refs = [f"hyperon-fact://{fact['id']}" for fact in facts]
    bundle = {
        "bundle_id": bundle_id,
        "site_id": SITE_ID,
        "cell_id": CELL_ID,
        "version": HYPERON_SOURCE_REF,
        "storage_ref": str(DATA_DIR),
        "facts_ref": f"hyperon://{SITE_ID}/{CELL_ID}/{run_id}/facts",
        "status": "ready" if decision.get("metta", {}).get("ok") else "degraded",
        "labels": {
            "provider": PROVIDER,
            "source_repo": HYPERON_SOURCE_REPO,
            "source_ref": HYPERON_SOURCE_REF,
            "source_commit": HYPERON_SOURCE_COMMIT,
            "runtime_version": HYPERON_VERSION,
            "project": project,
            "workerbee_lab": "ai-fabric",
        },
        "created_at": now,
        "updated_at": now,
    }
    trace = {
        "trace_id": _stable_id("trueagi-hyperon-query", [bundle_id, query_id, trace_id]),
        "bundle_id": bundle_id,
        "site_id": SITE_ID,
        "query_id": query_id,
        "query_kind": "advisory",
        "local_first": True,
        "warmed_refs": fact_refs,
        "promoted_refs": [result_ref],
        "fallback_sites": [],
        "result_ref": result_ref,
        "created_at": now,
    }
    signal = {
        "signal_id": _stable_id("trueagi-hyperon-signal", [trace["trace_id"], bundle_id]),
        "subject_type": "das-cell",
        "subject_id": bundle_id,
        "signal_kind": "symbolic-advisory",
        "continuity_ref": result_ref,
        "coherence_score": 0.62 if decision.get("status") == "blocked" else 0.82,
        "overload_state": "nominal",
        "review_gate": "operator_review",
        "advisory_trace_id": trace_id,
        "created_at": now,
    }
    return [
        {"kind": "das_cell_bundle", "payload": bundle},
        {"kind": "das_query_trace", "payload": trace},
        {"kind": "cognitive_signal", "payload": signal},
    ]


def _decision_trace(
    *,
    payload: dict[str, Any],
    decision: dict[str, Any],
    trace_id: str,
    request_id: str,
    now: str,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "request_id": request_id,
        "selected_lane": PROVIDER,
        "request_contract": {
            "request_id": request_id,
            "subject_type": str(payload.get("subject_type") or "fabric_advisory"),
            "subject_id": str(payload.get("subject") or payload.get("subject_id") or ""),
            "intent": str(payload.get("intent") or "advise_runtime"),
            "facts_ref": f"hyperon://{SITE_ID}/{CELL_ID}/{request_id}/facts",
            "locality_snapshot_ref": "",
            "max_candidates": int(payload.get("max_candidates") or 1),
            "time_budget_ms": int(payload.get("time_budget_ms") or 1000),
            "policy_mode": "advisory_only",
            "created_at": now,
        },
        "response_contract": {
            "request_id": request_id,
            "provider": PROVIDER,
            "status": decision["status"],
            "recommendation": decision["recommended_action"],
            "confidence": decision["confidence"],
            "evidence_refs": decision["evidence_refs"],
            "authoritative": False,
            "created_at": now,
        },
        "deterministic_baseline": {"controller_authority": "k1s", "authoritative": True},
        "advisory_response": decision,
        "accepted": None,
        "divergence_reason": "pending_operator_review",
        "replay_status": "recorded",
        "continuity_signals": {
            "provider": PROVIDER,
            "source_commit": HYPERON_SOURCE_COMMIT,
        },
        "coherence_signals": {
            "metta_ok": bool(decision.get("metta", {}).get("ok")),
            "risk_count": len(decision.get("risks") or []),
        },
        "created_at": now,
    }


def _append_f5_records(records: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_LOCK, F5_EVIDENCE_LOG.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _read_f5_records(limit: int = 30) -> list[dict[str, Any]]:
    try:
        lines = F5_EVIDENCE_LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-max(1, limit) :]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def evaluate_advisory(payload: dict[str, Any]) -> dict[str, Any]:
    now = _utc_now()
    facts = _normalize_facts(payload)
    metta = _run_metta_evaluation(facts)
    decision = _advisory_decision(payload, facts=facts, metta=metta, now=now)
    request_id = str(payload.get("request_id") or "")
    if not request_id:
        request_id = _stable_id("trueagi-hyperon-request", [payload, now])
    trace_id = str(payload.get("trace_id") or "")
    if not trace_id:
        trace_id = _stable_id("trueagi-hyperon-trace", [request_id, decision["decision_id"]])
    trace = _decision_trace(
        payload=payload,
        decision=decision,
        trace_id=trace_id,
        request_id=request_id,
        now=now,
    )
    records = _f5_evidence(
        payload=payload,
        decision=decision,
        facts=facts,
        trace_id=trace_id,
        now=now,
    )
    _append_f5_records(records)
    import_payload = {
        "api_version": K1S_ADVISORY_IMPORT_API_VERSION,
        "kind": "K1sFabricAdvisoryImport",
        "source": ADVISORY_API_VERSION,
        "decision_traces": [trace],
        "records": records,
    }
    return {
        "api_version": ADVISORY_API_VERSION,
        "kind": "TrueAGIHyperonAdvisoryEvaluation",
        "ok": True,
        "provider": PROVIDER,
        "provider_source": _provider_source(),
        "authoritative": False,
        "controller_authority": "k1s",
        "advisory_decision": decision,
        "decision_trace": trace,
        "f5_evidence": {
            "api_version": F5_EVIDENCE_API_VERSION,
            "kind": "AIFabricF5Evidence",
            "ok": True,
            "generated_at": now,
            "records": records,
        },
        "k1s_advisory_import": import_payload,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-fabric-hyperon-advisor/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            smoke = _run_metta_smoke()
            self._json(
                {
                    "ok": bool(smoke.get("ok")),
                    "service": "hyperon-advisor",
                    "provider": PROVIDER,
                    "provider_source": _provider_source(),
                    "metta_smoke": smoke,
                },
                status=200 if smoke.get("ok") else 503,
            )
            return
        if parsed.path == "/v1/f5/evidence":
            query = parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["30"])[0])
            except ValueError:
                limit = 30
            self._json(
                {
                    "ok": True,
                    "api_version": F5_EVIDENCE_API_VERSION,
                    "kind": "AIFabricF5Evidence",
                    "provider": PROVIDER,
                    "records": _read_f5_records(limit=limit),
                }
            )
            return
        self._json({"ok": False, "error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            self._json({"ok": False, "error": "invalid_json"}, status=400)
            return
        if parsed.path == "/v1/advisory/evaluate":
            self._json(evaluate_advisory(payload if isinstance(payload, dict) else {}))
            return
        self._json({"ok": False, "error": "not_found"}, status=404)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("HYPERON_ADVISOR_PORT", "8091"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
