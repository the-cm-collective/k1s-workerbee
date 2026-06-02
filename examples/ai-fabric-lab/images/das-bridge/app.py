#!/usr/bin/env python3
"""Hyperon DAS-backed symbolic evidence bridge for the AI fabric lab."""

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

DATA_DIR = Path(os.getenv("DAS_DATA_DIR", "/data/das"))
FACT_LOG = DATA_DIR / "facts.jsonl"
F5_EVIDENCE_LOG = DATA_DIR / "f5-evidence.jsonl"
BACKEND_NAME = os.getenv("AI_DAS_BACKEND", "hyperon-das")
SITE_ID = os.getenv("AI_FABRIC_SITE_ID", "site-a")
CELL_ID = os.getenv("AI_FABRIC_DAS_CELL_ID", "runtime")
FACT_NODE_TYPE = "Concept"
PREDICATE_NODE_TYPE = "Predicate"
FACT_LINK_TYPE = "ai-fabric:fact"
ADVISORY_DECISION_API_VERSION = "workerbee.ai-fabric.advisory-decision/v1"
PHASE_REPORT_SUBJECT = "k1s.fabric.phase_report"
PHASE_SUBJECT_PREFIX = "k1s.fabric.phase."
PHASE_EVIDENCE_MARKER = ".evidence."
ADAPTER_SUBJECT_PREFIX = "ai_fabric.adapter."
RELATIONSHIP_PREDICATES = (
    "owns_service",
    "depends_on",
    "serves_model",
    "requires_resource",
    "produced_artifact",
    "supports_advisory",
)
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
READY_STATES = {"available", "healthy", "ok", "ready", "true"}
TOKEN_RE = re.compile(r"[a-z0-9_.:-]+")
QUERY_STOPWORDS = {
    "about",
    "and",
    "for",
    "from",
    "how",
    "is",
    "the",
    "this",
    "what",
    "with",
}


class HyperonDASBackend:
    def __init__(self, backend_name: str) -> None:
        self.name = backend_name
        self.ready = False
        self.error: str | None = None
        self.about: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._das: Any | None = None
        self._node_type: Any | None = None
        self._link_type: Any | None = None
        self._initialize()

    def _initialize(self) -> None:
        if self.name != "hyperon-das":
            self.error = f"unsupported backend: {self.name}"
            return
        try:
            from hyperon_das import DistributedAtomSpace
            from hyperon_das_atomdb.database import LinkT, NodeT
        except Exception as exc:  # pragma: no cover - exercised in runtime image.
            self.error = str(exc)
            return
        try:
            self._das = DistributedAtomSpace()
            self._node_type = NodeT
            self._link_type = LinkT
            self.about = DistributedAtomSpace.about()
            self.ready = True
        except Exception as exc:  # pragma: no cover - defensive runtime path.
            self.error = str(exc)

    def replay(self, facts: list[dict[str, Any]]) -> None:
        for fact in facts:
            self.add_fact(fact)

    def add_fact(self, fact: dict[str, Any]) -> dict[str, Any]:
        if (
            not self.ready
            or self._das is None
            or self._node_type is None
            or self._link_type is None
        ):
            return {"ok": False, "error": self.error or "backend_unavailable"}
        with self._lock:
            try:
                subject = self._das.add_node(
                    self._node_type(
                        type=FACT_NODE_TYPE,
                        name=_node_name(fact["namespace"], fact["subject"]),
                        custom_attributes={"namespace": str(fact["namespace"])},
                    )
                )
                predicate = self._das.add_node(
                    self._node_type(type=PREDICATE_NODE_TYPE, name=str(fact["predicate"]))
                )
                obj = self._das.add_node(
                    self._node_type(type=FACT_NODE_TYPE, name=_object_name(fact["object"]))
                )
                link = self._das.add_link(
                    self._link_type(
                        type=FACT_LINK_TYPE,
                        targets=[predicate, subject, obj],
                        custom_attributes={"fact_id": str(fact["id"])},
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                return {"ok": False, "error": str(exc)}
            try:
                self._das.commit_changes()
                commit = {"ok": True}
            except NotImplementedError as exc:  # pragma: no cover - runtime package variance.
                commit = {"ok": False, "error": str(exc) or "not_implemented"}
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                commit = {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "commit": commit,
            "subject_handle": getattr(subject, "handle", None),
            "predicate_handle": getattr(predicate, "handle", None),
            "object_handle": getattr(obj, "handle", None),
            "link_handle": getattr(link, "handle", None),
        }

    def status(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "ready": self.ready,
            "error": self.error,
            "about": self.about,
        }
        if self.ready and self._das is not None:
            try:
                payload["atoms"] = self._das.count_atoms({"precise": True})
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                payload["atom_error"] = str(exc)
        return payload


BACKEND = HyperonDASBackend(BACKEND_NAME)
FACT_LOCK = threading.RLock()


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-fabric-das-bridge/0.4"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json(
                {
                    "ok": True,
                    "service": "das-bridge",
                    "backend": BACKEND.status(),
                    "fact_count": len(_read_facts()),
                    "f5_evidence_count": len(_read_f5_evidence()),
                }
            )
            return
        if parsed.path == "/v1/facts":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["100"])[0])
            self._json({"ok": True, "facts": _read_facts(limit=limit)})
            return
        if parsed.path == "/v1/f5/evidence":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["100"])[0])
            self._json({"ok": True, "records": _read_f5_evidence(limit=limit)})
            return
        if parsed.path == "/v1/relationships":
            self._json(
                {
                    "ok": True,
                    "api_version": "workerbee.ai-fabric.relationships/v1",
                    "predicates": list(RELATIONSHIP_PREDICATES),
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        payload = self._read_json()
        if parsed.path == "/v1/facts":
            fact = _append_fact(payload)
            backend = BACKEND.add_fact(fact)
            self._json({"ok": True, "fact": fact, "backend": backend}, status=201)
            return
        if parsed.path == "/v1/query":
            facts = _query_facts(payload)
            f5_evidence = _record_query_evidence(payload, facts)
            self._json(
                {
                    "ok": True,
                    "backend": BACKEND.status(),
                    "facts": facts,
                    "results": facts,
                    "f5_evidence": f5_evidence,
                }
            )
            return
        if parsed.path == "/v1/advisory/decision":
            decision = _advisory_decision(payload)
            self._json({"ok": True, "decision": decision})
            return
        if parsed.path == "/v1/import/runtime":
            facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
            imported = [_append_fact(item) for item in facts if isinstance(item, dict)]
            for fact in imported:
                BACKEND.add_fact(fact)
            self._json({"ok": True, "imported": len(imported), "facts": imported})
            return
        if parsed.path == "/v1/f5/replication-intent":
            replication = _record_replication_intent(payload)
            self._json({"ok": True, "replication": replication}, status=201)
            return
        self.send_error(404)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _append_fact(payload: dict[str, Any]) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fact = _normalize_fact(payload)
    with FACT_LOCK, FACT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(fact, sort_keys=True) + "\n")
    return fact


def _normalize_fact(payload: dict[str, Any]) -> dict[str, Any]:
    fact = {
        "namespace": str(payload.get("namespace") or "runtime"),
        "subject": str(payload.get("subject") or ""),
        "predicate": str(payload.get("predicate") or "observed"),
        "object": payload.get("object"),
        "source": str(payload.get("source") or "api"),
        "recorded_at": str(
            payload.get("recorded_at") or datetime.now(timezone.utc).isoformat()  # noqa: UP017
        ),
    }
    fact["id"] = _fact_id(fact)
    return fact


def _read_facts(*, limit: int | None = None) -> list[dict[str, Any]]:
    if not FACT_LOG.is_file():
        return []
    facts = []
    with FACT_LOCK:
        lines = FACT_LOG.read_text(encoding="utf-8").splitlines()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            facts.append(value)
    if limit is not None:
        return facts[-max(0, limit) :]
    return facts


def _append_f5_evidence(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "api_version": "workerbee.ai-fabric.f5-evidence-record/v1",
        "kind": kind,
        "payload": payload,
        "recorded_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
    }
    with FACT_LOCK, F5_EVIDENCE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def _read_f5_evidence(*, limit: int | None = None) -> list[dict[str, Any]]:
    if not F5_EVIDENCE_LOG.is_file():
        return []
    records = []
    with FACT_LOCK:
        lines = F5_EVIDENCE_LOG.read_text(encoding="utf-8").splitlines()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    if limit is not None:
        return records[-max(0, limit) :]
    return records


def _query_facts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    namespace = payload.get("namespace")
    subject = payload.get("subject")
    predicate = payload.get("predicate")
    text = str(payload.get("query") or "").lower()
    terms = [term for term in TOKEN_RE.findall(text) if term not in QUERY_STOPWORDS]
    limit = max(1, min(int(payload.get("limit") or 10), 100))
    matches = []
    for index, fact in enumerate(_read_facts()):
        if namespace is not None and fact.get("namespace") != namespace:
            continue
        if subject is not None and fact.get("subject") != subject:
            continue
        if predicate is not None and fact.get("predicate") != predicate:
            continue
        fact_text = json.dumps(fact, sort_keys=True).lower()
        score = sum(1 for term in terms if term in fact_text)
        if terms and score == 0:
            continue
        matches.append((score, index, fact))
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [fact for _, _, fact in matches[:limit]]


def _record_query_evidence(
    payload: dict[str, Any],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()  # noqa: UP017
    bundle = _das_cell_bundle(now)
    trace = _das_query_trace(payload, facts, bundle, now)
    signal = _cognitive_signal(trace, bundle, now)
    _append_f5_evidence("das_cell_bundle", bundle)
    _append_f5_evidence("das_query_trace", trace)
    _append_f5_evidence("cognitive_signal", signal)
    return {
        "api_version": "workerbee.ai-fabric.f5-query-evidence/v1",
        "cell_bundle": bundle,
        "query_trace": trace,
        "cognitive_signal": signal,
    }


def _record_replication_intent(payload: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()  # noqa: UP017
    bundle = _das_cell_bundle(now)
    peer_site = str(payload.get("target_site_id") or payload.get("peer_site_id") or "site-b")
    replication = {
        "replication_id": _stable_id("das-replication", [bundle["bundle_id"], SITE_ID, peer_site]),
        "bundle_id": bundle["bundle_id"],
        "source_site_id": SITE_ID,
        "target_site_id": peer_site,
        "mode": str(payload.get("mode") or "controlled"),
        "status": str(payload.get("status") or "planned"),
        "approved_by": str(payload.get("approved_by") or "operator"),
        "reason": str(
            payload.get("reason") or "warm peer DAS cell without making WAN a hot query path"
        ),
        "created_at": now,
        "updated_at": now,
    }
    _append_f5_evidence("das_cell_bundle", bundle)
    _append_f5_evidence("das_replication", replication)
    return replication


def _advisory_decision(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or "")
    intent = str(payload.get("intent") or "advise")
    limit = max(1, min(int(payload.get("limit") or 10), 50))
    use_stored_facts = payload.get("use_stored_facts") is not False
    supplied_facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
    supplied_facts = [
        _normalize_fact(fact) for fact in supplied_facts if isinstance(fact, dict)
    ]
    subject = _advisory_subject(payload=payload, facts=supplied_facts, query=query)
    facts = supplied_facts[:limit]
    if not facts and subject and use_stored_facts:
        facts = _query_facts({"subject": subject, "limit": limit})
    if not facts and use_stored_facts:
        facts = _query_facts({"query": query, "limit": limit})
    subject = subject or _advisory_subject(payload=payload, facts=facts, query=query)
    subject = subject or "ai_fabric.lab"
    evidence_refs = _decision_evidence_refs(facts)
    blocked_conditions = _decision_blocked_conditions(subject=subject, facts=facts)
    risks = _decision_risks(
        subject=subject,
        facts=facts,
        blocked_conditions=blocked_conditions,
    )
    status = "blocked" if blocked_conditions else "review"
    if not facts:
        recommended_action = "defer action until DAS runtime evidence is imported"
        confidence = 0.2
    elif blocked_conditions:
        recommended_action = "resolve blocked symbolic conditions before changing runtime state"
        confidence = 0.45
    else:
        recommended_action = (
            f"review {subject} with the attached symbolic evidence and verify live k1s state"
        )
        confidence = 0.7
    now = datetime.now(timezone.utc).isoformat()  # noqa: UP017
    decision = {
        "api_version": ADVISORY_DECISION_API_VERSION,
        "decision_id": _stable_id(
            "advisory-decision",
            [subject, intent, query, *[str(fact.get("id") or "") for fact in facts[:8]]],
        ),
        "subject": subject,
        "intent": intent,
        "query": query,
        "status": status,
        "recommended_action": recommended_action,
        "confidence": confidence,
        "evidence_refs": evidence_refs,
        "risks": risks,
        "blocked_conditions": blocked_conditions,
        "authoritative": False,
        "controller_authority": "k1s",
        "facts": facts,
        "created_at": now,
    }
    _append_f5_evidence("advisory_decision", decision)
    return decision


def _advisory_subject(
    *,
    payload: dict[str, Any],
    facts: list[dict[str, Any]],
    query: str,
) -> str | None:
    for key in ("subject", "subject_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    query_text = query.lower()
    candidates: list[str] = []
    for fact in [*facts, *_read_facts()]:
        subject = fact.get("subject")
        obj = fact.get("object")
        if isinstance(subject, str) and subject.startswith("ai_fabric.service."):
            candidates.append(subject)
        if isinstance(obj, str) and obj.startswith("ai_fabric.service."):
            candidates.append(obj)
    for candidate in candidates:
        service_name = candidate.rsplit(".", 1)[-1].lower()
        if service_name in query_text or service_name.replace("-", " ") in query_text:
            return candidate
    if facts and isinstance(facts[0].get("subject"), str):
        return str(facts[0]["subject"])
    return None


def _decision_evidence_refs(facts: list[dict[str, Any]]) -> list[str]:
    return [f"das-fact://{fact['id']}" for fact in facts if fact.get("id")]


def _decision_blocked_conditions(
    *,
    subject: str,
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    phase_context = subject == PHASE_REPORT_SUBJECT or _is_phase_subject(subject)
    adapter_context = _is_adapter_subject(subject)

    def add_condition(condition: dict[str, Any]) -> None:
        evidence_ref = condition.get("evidence_ref")
        key = (
            str(condition.get("condition") or ""),
            str(condition.get("state") or ""),
            evidence_ref if isinstance(evidence_ref, str) else None,
        )
        if key in seen:
            return
        seen.add(key)
        conditions.append(condition)

    for fact in facts:
        fact_subject = str(fact.get("subject") or "")
        predicate = str(fact.get("predicate") or "")
        if phase_context and _phase_fact_applies(subject, fact_subject):
            if predicate == "gate_ready":
                state = _condition_state(fact.get("object"))
                if state in DEGRADED_STATES:
                    add_condition(
                        _phase_gate_blocked_condition(
                            fact,
                            _phase_gate_blockers(fact_subject, facts),
                        )
                    )
                continue
            if predicate == "present" and _is_phase_evidence_subject(fact_subject):
                state = _condition_state(fact.get("object"))
                if state in DEGRADED_STATES:
                    add_condition(_blocked_condition(fact, state))
                continue
            if predicate == "status":
                state = _condition_state(fact.get("object"))
                if state in DEGRADED_STATES:
                    add_condition(_blocked_condition(fact, state))
                continue
            if fact_subject == PHASE_REPORT_SUBJECT and predicate == "artifact_state":
                state = _condition_state(fact.get("object"))
                if state in DEGRADED_STATES:
                    add_condition(_blocked_condition(fact, state))
                continue
        if _is_phase_fact_subject(fact_subject):
            continue
        if adapter_context and fact_subject == subject and predicate in {
            "readiness",
            "status",
            "adapter_state",
            "preflight_state",
        }:
            state = _condition_state(fact.get("object"))
            if state in DEGRADED_STATES:
                add_condition(_blocked_condition(fact, state))
            continue
        if predicate in {"readiness", "status", "host_alias_health", "model_lane_readiness"}:
            state = _condition_state(fact.get("object"))
            if state in DEGRADED_STATES:
                add_condition(_blocked_condition(fact, state))
        if predicate == "artifact_state":
            state = _condition_state(fact.get("object"))
            if state in DEGRADED_STATES:
                add_condition(_blocked_condition(fact, state))
    dependencies = [
        str(fact.get("object"))
        for fact in facts
        if fact.get("subject") == subject
        and fact.get("predicate") == "depends_on"
        and isinstance(fact.get("object"), str)
    ]
    for dependency in dependencies:
        for fact in facts:
            if fact.get("subject") != dependency:
                continue
            predicate = str(fact.get("predicate") or "")
            if predicate not in {"readiness", "status", "host_alias_health"}:
                continue
            state = _condition_state(fact.get("object"))
            if state in DEGRADED_STATES:
                add_condition(_blocked_condition(fact, state, dependency=dependency))
    return conditions


def _blocked_condition(
    fact: dict[str, Any],
    state: str,
    *,
    dependency: str | None = None,
) -> dict[str, Any]:
    subject = str(dependency or fact.get("subject") or "unknown")
    predicate = str(fact.get("predicate") or "state")
    return {
        "condition": f"{subject}.{predicate}",
        "state": state,
        "reason": f"{subject} reports {predicate}={state}",
        "evidence_ref": f"das-fact://{fact['id']}" if fact.get("id") else None,
    }


def _phase_gate_blocked_condition(
    fact: dict[str, Any],
    blockers: list[str],
) -> dict[str, Any]:
    subject = str(fact.get("subject") or "unknown")
    reason = f"{subject} reports gate_ready=false"
    if blockers:
        reason = f"{reason}; blocked_by={','.join(blockers)}"
    return {
        "condition": f"{subject}.gate_ready",
        "state": "blocked",
        "reason": reason,
        "evidence_ref": f"das-fact://{fact['id']}" if fact.get("id") else None,
    }


def _decision_risks(
    *,
    subject: str,
    facts: list[dict[str, Any]],
    blocked_conditions: list[dict[str, Any]],
) -> list[str]:
    risks: list[str] = []
    if not facts:
        risks.append("missing_symbolic_evidence")
    if blocked_conditions:
        risks.append("symbolic_blocked_condition")
    if any(
        fact.get("predicate") == "artifact_state"
        and _condition_state(fact.get("object")) in DEGRADED_STATES
        for fact in facts
    ):
        risks.append("validation_artifact_unhealthy")
    if _phase_gate_blocked(subject, facts):
        risks.append("fabric_phase_gate_blocked")
    if _missing_phase_evidence(subject, facts):
        risks.append("missing_phase_evidence")
    if _phase_report_stale(subject, facts):
        risks.append("phase_report_stale")
    if _lora_adapter_not_ready(subject, facts):
        risks.append("lora_adapter_not_ready")
    if subject.startswith("ai_fabric.service.") and not any(
        fact.get("subject") == subject and fact.get("predicate") == "depends_on"
        for fact in facts
    ):
        risks.append("dependency_context_incomplete")
    if not any(fact.get("predicate") in RELATIONSHIP_PREDICATES for fact in facts):
        risks.append("relationship_context_sparse")
    return risks


def _is_adapter_subject(subject: str) -> bool:
    return subject.startswith(ADAPTER_SUBJECT_PREFIX)


def _is_phase_subject(subject: str) -> bool:
    return (
        subject.startswith(PHASE_SUBJECT_PREFIX)
        and PHASE_EVIDENCE_MARKER not in subject
    )


def _is_phase_evidence_subject(subject: str) -> bool:
    return (
        subject.startswith(PHASE_SUBJECT_PREFIX)
        and PHASE_EVIDENCE_MARKER in subject
    )


def _is_phase_fact_subject(subject: str) -> bool:
    return subject == PHASE_REPORT_SUBJECT or subject.startswith(PHASE_SUBJECT_PREFIX)


def _phase_id_from_subject(subject: str) -> str | None:
    if not subject.startswith(PHASE_SUBJECT_PREFIX):
        return None
    tail = subject[len(PHASE_SUBJECT_PREFIX) :]
    return tail.split(".", 1)[0] if tail else None


def _phase_fact_applies(decision_subject: str, fact_subject: str) -> bool:
    if decision_subject == PHASE_REPORT_SUBJECT:
        return _is_phase_fact_subject(fact_subject)
    if not _is_phase_subject(decision_subject):
        return False
    return _phase_id_from_subject(decision_subject) == _phase_id_from_subject(fact_subject)


def _phase_gate_blockers(phase_subject: str, facts: list[dict[str, Any]]) -> list[str]:
    phase_id = _phase_id_from_subject(phase_subject)
    if not phase_id:
        return []
    canonical = f"{PHASE_SUBJECT_PREFIX}{phase_id}"
    blockers = []
    for fact in facts:
        if fact.get("subject") != canonical or fact.get("predicate") != "blocked_by":
            continue
        blocker = fact.get("object")
        if isinstance(blocker, str) and blocker not in blockers:
            blockers.append(blocker)
    return blockers


def _phase_gate_blocked(subject: str, facts: list[dict[str, Any]]) -> bool:
    if subject != PHASE_REPORT_SUBJECT and not _is_phase_subject(subject):
        return False
    for fact in facts:
        fact_subject = str(fact.get("subject") or "")
        if not _phase_fact_applies(subject, fact_subject):
            continue
        predicate = fact.get("predicate")
        if predicate == "blocked_by":
            return True
        if predicate == "gate_ready" and _condition_state(fact.get("object")) in DEGRADED_STATES:
            return True
    return False


def _missing_phase_evidence(subject: str, facts: list[dict[str, Any]]) -> bool:
    if subject != PHASE_REPORT_SUBJECT and not _is_phase_subject(subject):
        return False
    for fact in facts:
        fact_subject = str(fact.get("subject") or "")
        if not _phase_fact_applies(subject, fact_subject):
            continue
        predicate = fact.get("predicate")
        if predicate == "present" and _is_phase_evidence_subject(fact_subject):
            if _condition_state(fact.get("object")) in DEGRADED_STATES:
                return True
        if predicate == "status" and _condition_state(fact.get("object")) in DEGRADED_STATES:
            return True
    return False


def _phase_report_stale(subject: str, facts: list[dict[str, Any]]) -> bool:
    if subject != PHASE_REPORT_SUBJECT:
        return False
    return any(
        fact.get("subject") == PHASE_REPORT_SUBJECT
        and fact.get("predicate") == "artifact_state"
        and _condition_state(fact.get("object")) in DEGRADED_STATES
        for fact in facts
    )


def _lora_adapter_not_ready(subject: str, facts: list[dict[str, Any]]) -> bool:
    if not _is_adapter_subject(subject):
        return False
    return any(
        fact.get("subject") == subject
        and fact.get("predicate") in {"readiness", "status", "adapter_state", "preflight_state"}
        and _condition_state(fact.get("object")) in DEGRADED_STATES
        for fact in facts
    )


def _condition_state(value: Any) -> str:
    if isinstance(value, bool):
        return "ready" if value else "unhealthy"
    if isinstance(value, str):
        lowered = value.strip().lower().replace(" ", "_")
        if lowered in READY_STATES or lowered in DEGRADED_STATES:
            return lowered
        return "unknown"
    if isinstance(value, dict):
        if value.get("ok") is False:
            return "unhealthy"
        for key in ("readiness", "status", "state", "health"):
            state = value.get(key)
            if isinstance(state, str):
                return _condition_state(state)
        if value.get("ready") is False:
            return "not_ready"
        if value.get("ready") is True or value.get("ok") is True:
            return "ready"
    return "unknown"


def _das_cell_bundle(now: str) -> dict[str, Any]:
    bundle_id = _stable_id("das-bundle", [SITE_ID, CELL_ID, str(DATA_DIR)])
    return {
        "bundle_id": bundle_id,
        "site_id": SITE_ID,
        "cell_id": CELL_ID,
        "version": now[:10],
        "storage_ref": str(DATA_DIR),
        "facts_ref": f"das://{SITE_ID}/{CELL_ID}/facts.jsonl",
        "status": "ready",
        "labels": {"backend": BACKEND_NAME, "workerbee_lab": "ai-fabric"},
        "created_at": now,
        "updated_at": now,
    }


def _das_query_trace(
    payload: dict[str, Any],
    facts: list[dict[str, Any]],
    bundle: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    query = str(payload.get("query") or payload.get("subject") or "")
    query_id = str(payload.get("query_id") or _stable_id("query", [query]))
    trace_id = str(
        payload.get("trace_id") or _stable_id("das-query", [bundle["bundle_id"], query_id])
    )
    promoted_refs = (
        payload.get("promoted_refs") if isinstance(payload.get("promoted_refs"), list) else []
    )
    fallback_sites = (
        payload.get("fallback_sites") if isinstance(payload.get("fallback_sites"), list) else []
    )
    warmed_refs = [f"das-fact://{fact['id']}" for fact in facts[:5] if fact.get("id")]
    if not warmed_refs:
        warmed_refs = [str(bundle["facts_ref"])]
    return {
        "trace_id": trace_id,
        "bundle_id": str(bundle["bundle_id"]),
        "site_id": SITE_ID,
        "query_id": query_id,
        "query_kind": str(payload.get("query_kind") or "advisory"),
        "local_first": True,
        "warmed_refs": warmed_refs,
        "promoted_refs": promoted_refs,
        "fallback_sites": fallback_sites,
        "result_ref": f"das://{SITE_ID}/{CELL_ID}/query/{trace_id}",
        "created_at": now,
    }


def _cognitive_signal(
    trace: dict[str, Any],
    bundle: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    return {
        "signal_id": _stable_id("cognitive-signal", [trace["trace_id"], bundle["bundle_id"]]),
        "subject_type": "das-cell",
        "subject_id": str(bundle["bundle_id"]),
        "signal_kind": "continuity",
        "continuity_ref": str(trace["result_ref"]),
        "coherence_score": 1.0,
        "overload_state": "nominal",
        "review_gate": "operator_review",
        "advisory_trace_id": str(trace["trace_id"]),
        "created_at": now,
    }


def _fact_id(fact: dict[str, Any]) -> str:
    stable = {
        "namespace": fact["namespace"],
        "subject": fact["subject"],
        "predicate": fact["predicate"],
        "object": fact["object"],
        "source": fact["source"],
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _stable_id(prefix: str, parts: list[str]) -> str:
    digest = hashlib.sha256("\n".join(str(part) for part in parts).encode()).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _node_name(namespace: str, subject: str) -> str:
    return f"{namespace}:{subject}"


def _object_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKEND.replay(_read_facts())
    port = int(os.getenv("PORT", "8081"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
