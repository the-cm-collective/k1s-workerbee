"""Fail-closed Agent PBX controlled-run contract and strict adapter seam.

The OSS package owns validation, durable idempotency, bounded receipts, and the
MCP lifecycle contract.  It deliberately does not implement the isolation
appliance.  A platform package must provide one adapter through the
``workerbee.controlled_run_adapter`` entry-point group and attest every strict
capability before the tools are advertised.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from workerbee.contract import WorkerBeeError
from workerbee.locks import FileLock

CONTROLLED_RUN_MODE_ENV = "WORKERBEE_CONTROLLED_RUN_MODE"
CONTROLLED_RUN_ADAPTER_GROUP = "workerbee.controlled_run_adapter"
CONTROLLED_RUN_API_VERSION = "workerbee.controlled-run/v1"
CONTROLLED_RUN_RECEIPT_VERSION = "workerbee.controlled-run-receipt/v1"

CONTROLLED_RUN_TOOL_NAMES = (
    "workerbee_v1_controlled_run_preflight",
    "workerbee_v1_controlled_run_start",
    "workerbee_v1_controlled_run_status",
    "workerbee_v1_controlled_run_cancel",
    "workerbee_v1_controlled_run_receipt",
)

CONTROLLED_RUN_ACTIVE_STATES = frozenset(
    {
        "provisioning",
        "starting",
        "canary",
        "running",
        "collecting",
        "cleaning",
        "recovery_required",
    }
)
CONTROLLED_RUN_TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled", "cancelled"})
CONTROLLED_RUN_STATES = CONTROLLED_RUN_ACTIVE_STATES | CONTROLLED_RUN_TERMINAL_STATES
_AGENT_PBX_STATUS_ALIASES = {
    "provisioning": "preparing",
    "starting": "preparing",
    "canary": "ready",
    "recovery_required": "recovering",
}

CONTROLLED_RUN_REQUIRED_ADAPTER_CAPABILITIES = (
    "strict_appliance",
    "immutable_commit_export",
    "caller_worktree_read_only",
    "owner_private_run_root",
    "loopback_tcp",
    "pathname_af_unix",
    "dns_denied",
    "non_loopback_denied",
    "abstract_unix_denied",
    "host_control_sockets_denied",
    "empty_environment",
    "ambient_credentials_denied",
    "ambient_config_denied",
    "package_install_denied",
    "package_index_denied",
    "runtime_fallback_denied",
    "pinned_tools",
    "resource_limits",
    "bounded_evidence",
    "durable_journal",
    "crash_recovery",
    "always_cleanup",
    "residue_reporting",
)

MAX_REQUEST_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 192 * 1024
MAX_RESIDUE_ITEMS = 128
MAX_ARTIFACTS = 256
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_WALL_SECONDS = 86_400
MAX_MEMORY_MIB = 262_144
MAX_OUTPUT_BYTES = 1024 * 1024 * 1024
MAX_CONCURRENCY = 8

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_GIT_OID = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|private[_-]?key|secret|token)", re.I
)
_FORBIDDEN_REQUEST_KEYS = frozenset(
    {"argv", "command", "commands", "env", "environment", "shell", "script"}
)
_REQUEST_KEYS = frozenset(
    {
        "request_id",
        "idempotency_key",
        "request_sha256",
        "root_operator_agent_id",
        "target_fork_agent_id",
        "campaign_id",
        "workspace_binding_id",
        "profile",
        "profile_sha256",
        "baseline_commit",
        "candidate_commit",
        "recipe_id",
        "tool_manifest_sha256",
    }
)
_AGENT_PBX_PROFILE_DIGEST_KEYS = (
    "profile_id",
    "version",
    "label",
    "enabled",
    "backend",
    "required_authority",
    "allowed_workerbee_backends",
    "allowed_projects",
    "source_mode",
    "network_mode",
    "external_egress",
    "credentials",
    "recipe_id",
    "tool_manifest_sha256",
    "max_seconds",
    "max_memory_mib",
    "max_output_bytes",
    "max_concurrency",
    "approval_mode",
    "lease_max_uses",
    "lease_ttl_seconds",
    "retention_seconds",
    "cleanup_required",
    "allow_host_fallback",
)


@dataclass(frozen=True, slots=True)
class ControlledRunRequest:
    request_id: str
    idempotency_key: str
    request_sha256: str
    root_operator_agent_id: str
    target_fork_agent_id: str
    campaign_id: str
    workspace_binding_id: str
    profile: dict[str, Any]
    profile_sha256: str
    baseline_commit: str
    candidate_commit: str
    recipe_id: str
    tool_manifest_sha256: str
    canonical_payload_sha256: str

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("canonical_payload_sha256", None)
        return payload


@runtime_checkable
class ControlledRunAdapter(Protocol):
    """Strict appliance operations supplied by the platform-debug package."""

    name: str
    version: str

    def capabilities(self) -> Mapping[str, Any]: ...

    def recipes(self) -> Mapping[str, Mapping[str, Any]]: ...

    def preflight(
        self,
        *,
        request: Mapping[str, Any],
        recipe: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def start(
        self,
        *,
        run_id: str,
        run_root: Path,
        scratch_dir: Path,
        request: Mapping[str, Any],
        recipe: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def status(self, *, run_id: str) -> Mapping[str, Any]: ...

    def cancel(self, *, run_id: str, reason: str) -> Mapping[str, Any]: ...

    def receipt(self, *, run_id: str) -> Mapping[str, Any]: ...

    def recover(
        self,
        *,
        run_id: str,
        journal: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class UnavailableControlledRunAdapter:
    """Default OSS adapter: explicit, unavailable, and incapable of execution."""

    name = "unavailable"
    version = "0"

    def __init__(self, reason: str = "strict controlled-run adapter is not installed") -> None:
        self.reason = reason

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "available": False,
            "reason": self.reason,
            **dict.fromkeys(CONTROLLED_RUN_REQUIRED_ADAPTER_CAPABILITIES, False),
        }

    def recipes(self) -> Mapping[str, Mapping[str, Any]]:
        return {}

    def _raise(self) -> None:
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_ADAPTER_UNAVAILABLE",
            self.reason,
            remediation=(
                "Install and select the platform-debug strict controlled-run adapter, then "
                f"set {CONTROLLED_RUN_MODE_ENV}=enabled."
            ),
        )

    def preflight(self, **_kwargs: Any) -> Mapping[str, Any]:
        self._raise()

    def start(self, **_kwargs: Any) -> Mapping[str, Any]:
        self._raise()

    def status(self, **_kwargs: Any) -> Mapping[str, Any]:
        self._raise()

    def cancel(self, **_kwargs: Any) -> Mapping[str, Any]:
        self._raise()

    def receipt(self, **_kwargs: Any) -> Mapping[str, Any]:
        self._raise()

    def recover(self, **_kwargs: Any) -> Mapping[str, Any]:
        self._raise()


def load_controlled_run_adapter() -> ControlledRunAdapter:
    """Load exactly one platform adapter through the narrow packaging seam."""

    matches = list(entry_points(group=CONTROLLED_RUN_ADAPTER_GROUP))
    if not matches:
        return UnavailableControlledRunAdapter()
    if len(matches) != 1:
        return UnavailableControlledRunAdapter(
            f"expected one {CONTROLLED_RUN_ADAPTER_GROUP!r} entry point; found {len(matches)}"
        )
    try:
        factory = matches[0].load()
        adapter = factory() if callable(factory) else factory
    except Exception as exc:  # noqa: BLE001 - normalize private adapter load failures
        return UnavailableControlledRunAdapter(
            f"strict controlled-run adapter failed to load ({type(exc).__name__})"
        )
    if not isinstance(adapter, ControlledRunAdapter):
        return UnavailableControlledRunAdapter(
            "strict controlled-run entry point does not implement the required adapter protocol"
        )
    return adapter


class ControlledRunService:
    """Validate and journal the fixed-recipe controlled-run lifecycle."""

    def __init__(
        self,
        *,
        state_root: Path,
        adapter: ControlledRunAdapter | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.state_root = state_root.expanduser().resolve()
        self.environ = dict(os.environ if environ is None else environ)
        self.mode = str(self.environ.get(CONTROLLED_RUN_MODE_ENV) or "disabled").strip().lower()
        if self.mode not in {"disabled", "enabled"}:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_CONFIG_INVALID",
                f"{CONTROLLED_RUN_MODE_ENV} must be 'disabled' or 'enabled'.",
            )
        self.adapter: ControlledRunAdapter = (
            adapter
            if adapter is not None
            else (
                load_controlled_run_adapter()
                if self.mode == "enabled"
                else UnavailableControlledRunAdapter("controlled runs are disabled by policy")
            )
        )
        self.root = self.state_root / "controlled-runs"
        self.runs_root = self.root / "runs"
        self.idempotency_root = self.root / "idempotency"
        self.lock_path = self.root / ".lock"

    def capabilities(self) -> dict[str, Any]:
        enabled = self.mode == "enabled"
        if enabled:
            try:
                adapter_capabilities = _plain_mapping(self.adapter.capabilities())
            except Exception as exc:  # noqa: BLE001 - keep private adapter errors stable
                adapter_capabilities = {
                    "available": False,
                    "error_code": "WORKERBEE_CONTROLLED_RUN_ADAPTER_ERROR",
                    "error_type": type(exc).__name__,
                }
            missing = [
                name
                for name in CONTROLLED_RUN_REQUIRED_ADAPTER_CAPABILITIES
                if adapter_capabilities.get(name) is not True
            ]
            recipes = self._recipes(strict=False)
        else:
            adapter_capabilities = {"available": False, "policy_enabled": False}
            missing = list(CONTROLLED_RUN_REQUIRED_ADAPTER_CAPABILITIES)
            recipes = {}
        available = enabled and not missing and bool(recipes)
        if not enabled:
            reason = "disabled by default; set WORKERBEE_CONTROLLED_RUN_MODE=enabled"
        elif missing:
            reason = "strict adapter is missing required capabilities: " + ", ".join(missing)
        elif not recipes:
            reason = "strict adapter exposes no fixed recipes"
        else:
            reason = "ready"
        return {
            "api_version": CONTROLLED_RUN_API_VERSION,
            "enabled": enabled,
            "available": available,
            "reason": reason,
            "adapter": {
                "name": str(getattr(self.adapter, "name", "unknown")),
                "version": str(getattr(self.adapter, "version", "unknown")),
                "capabilities": adapter_capabilities,
                "missing_capabilities": missing,
            },
            "recipes": sorted(recipes) if available else [],
            "advertised_tools": list(CONTROLLED_RUN_TOOL_NAMES) if available else [],
            "states": sorted(CONTROLLED_RUN_STATES),
            "default_disabled": True,
        }

    @property
    def available(self) -> bool:
        return bool(self.capabilities()["available"])

    def preflight(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = self._request(payload)
        recipe, policy = self._validated_recipe_policy(request)
        try:
            attestation = _plain_mapping(
                self.adapter.preflight(
                    request=request.public_dict(),
                    recipe=recipe,
                    policy=policy,
                )
            )
        except Exception as exc:  # noqa: BLE001 - normalize private adapter failures
            raise _adapter_error("preflight", exc) from None
        source = self._validated_source_attestation(request, attestation)
        return {
            "ok": True,
            "api_version": CONTROLLED_RUN_API_VERSION,
            "request_id": request.request_id,
            "request_sha256": request.request_sha256,
            "profile_sha256": request.profile_sha256,
            "tool_manifest_sha256": request.tool_manifest_sha256,
            "workspace_binding_id": request.workspace_binding_id,
            "recipe_id": request.recipe_id,
            "adapter": {
                "name": str(getattr(self.adapter, "name", "unknown")),
                "version": str(getattr(self.adapter, "version", "unknown")),
            },
            "policy": policy,
            "source": source,
        }

    def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = self._request(payload)
        preflight = self.preflight(request.public_dict())
        recipe, policy = self._validated_recipe_policy(request)
        self._ensure_state_root()
        with FileLock(self.lock_path, label="controlled runs"):
            existing = self._idempotent_record(request)
            if existing is not None:
                return self._start_response(existing, reused=True)
            self._enforce_concurrency(policy)
            run_id = (
                "crun-" + hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()[:24]
            )
            run_root = self.runs_root / run_id
            scratch_dir = run_root / "scratch"
            evidence_dir = run_root / "evidence"
            _private_dir(run_root)
            _private_dir(scratch_dir)
            _private_dir(evidence_dir)
            journal = {
                "api_version": CONTROLLED_RUN_API_VERSION,
                "run_id": run_id,
                "workspace_id": None,
                "state": "provisioning",
                "request_id": request.request_id,
                "request_sha256": request.request_sha256,
                "canonical_payload_sha256": request.canonical_payload_sha256,
                "idempotency_key_sha256": hashlib.sha256(
                    request.idempotency_key.encode("utf-8")
                ).hexdigest(),
                "workspace_binding_id": request.workspace_binding_id,
                "root_operator_agent_id": request.root_operator_agent_id,
                "target_fork_agent_id": request.target_fork_agent_id,
                "campaign_id": request.campaign_id,
                "recipe_id": request.recipe_id,
                "profile_sha256": request.profile_sha256,
                "tool_manifest_sha256": request.tool_manifest_sha256,
                "baseline_commit": request.baseline_commit,
                "candidate_commit": request.candidate_commit,
                "run_root": str(run_root),
                "scratch_dir": str(scratch_dir),
                "evidence_dir": str(evidence_dir),
                "created_at": time.time(),
                "updated_at": time.time(),
                "source": preflight["source"],
                "policy_sha256": _sha256_json(policy),
                "limits": policy["limits"],
                "history": [{"state": "provisioning", "at": time.time()}],
            }
            self._write_journal(run_id, journal)
            self._write_idempotency(request, run_id)
            try:
                started = _plain_mapping(
                    self.adapter.start(
                        run_id=run_id,
                        run_root=run_root,
                        scratch_dir=scratch_dir,
                        request=request.public_dict(),
                        recipe=recipe,
                        policy=policy,
                    )
                )
                state = _state(started)
                workspace_id = _identifier(started.get("workspace_id"), "workspace_id")
                journal = self._transition(
                    journal,
                    state,
                    workspace_id=workspace_id,
                    adapter_result=_bounded_mapping(started, MAX_EVIDENCE_BYTES),
                )
                self._write_journal(run_id, journal)
            except Exception as exc:
                journal = self._recover_after_start_failure(journal, exc)
                self._write_journal(run_id, journal)
                raise _adapter_error("start", exc) from None
            return self._start_response(journal, reused=False)

    def status(self, run_id: str) -> dict[str, Any]:
        run_id = _identifier(run_id, "run_id")
        self._require_available()
        journal = self._read_journal(run_id)
        try:
            observed = _plain_mapping(self.adapter.status(run_id=run_id))
            state = _state(observed)
            if state in CONTROLLED_RUN_TERMINAL_STATES:
                receipt = self._validated_receipt(run_id, journal)
                if not receipt["cleanup"]["complete"] or receipt["cleanup"]["residue"]:
                    journal, _cleanup = self._recover_after_adapter_failure(
                        journal,
                        _error(
                            "WORKERBEE_CONTROLLED_RUN_CLEANUP_INCOMPLETE",
                            "Terminal state did not have a zero-residue cleanup receipt.",
                        ),
                        operation="status_cleanup",
                    )
                    self._write_journal(run_id, journal)
                    state = str(journal["state"])
                    observed = {
                        "state": state,
                        "error_code": "WORKERBEE_CONTROLLED_RUN_CLEANUP_INCOMPLETE",
                    }
        except Exception as exc:
            journal, _cleanup = self._recover_after_adapter_failure(
                journal,
                exc,
                operation="status",
            )
            self._write_journal(run_id, journal)
            if isinstance(exc, WorkerBeeError):
                raise
            raise _adapter_error("status", exc) from None
        workspace_id = str(observed.get("workspace_id") or journal.get("workspace_id") or "")
        journal = self._transition(
            journal,
            state,
            workspace_id=workspace_id or None,
            adapter_result=_bounded_mapping(observed, MAX_EVIDENCE_BYTES),
        )
        self._write_journal(run_id, journal)
        return {
            "api_version": CONTROLLED_RUN_API_VERSION,
            "run_id": run_id,
            "workspace_id": journal.get("workspace_id"),
            "state": state,
            "status": _agent_pbx_status(state),
            "updated_at": journal["updated_at"],
        }

    def cancel(self, run_id: str, reason: str | None = None) -> dict[str, Any]:
        run_id = _identifier(run_id, "run_id")
        self._require_available()
        journal = self._read_journal(run_id)
        if str(journal.get("state")) in CONTROLLED_RUN_TERMINAL_STATES:
            receipt = self._validated_receipt(run_id, journal)
            return {
                "api_version": CONTROLLED_RUN_API_VERSION,
                "run_id": run_id,
                "workspace_id": journal.get("workspace_id"),
                "state": str(journal["state"]),
                "status": _agent_pbx_status(str(journal["state"])),
                "cancel_reused": True,
                "cleanup": receipt["cleanup"],
            }
        try:
            observed = _plain_mapping(
                self.adapter.cancel(run_id=run_id, reason=str(reason or "")[:1000])
            )
            state = _state(observed)
            receipt = self._validated_receipt(run_id, journal)
            if not receipt["cleanup"]["complete"] or receipt["cleanup"]["residue"]:
                journal, cleanup = self._recover_after_adapter_failure(
                    journal,
                    _error(
                        "WORKERBEE_CONTROLLED_RUN_CLEANUP_INCOMPLETE",
                        "Cancel did not produce a zero-residue cleanup receipt.",
                    ),
                    operation="cancel_cleanup",
                )
                self._write_journal(run_id, journal)
                state = str(journal["state"])
                return {
                    "api_version": CONTROLLED_RUN_API_VERSION,
                    "run_id": run_id,
                    "workspace_id": journal.get("workspace_id"),
                    "state": state,
                    "status": _agent_pbx_status(state),
                    "cancel_reused": False,
                    "cleanup": cleanup,
                }
            if state not in {"canceled", "cancelled", "failed"}:
                state = "canceled"
        except Exception as exc:
            journal, _cleanup = self._recover_after_adapter_failure(
                journal,
                exc,
                operation="cancel",
            )
            self._write_journal(run_id, journal)
            if isinstance(exc, WorkerBeeError):
                raise
            raise _adapter_error("cancel", exc) from None
        journal = self._transition(
            journal,
            state,
            adapter_result=_bounded_mapping(observed, MAX_EVIDENCE_BYTES),
        )
        self._write_journal(run_id, journal)
        return {
            "api_version": CONTROLLED_RUN_API_VERSION,
            "run_id": run_id,
            "workspace_id": journal.get("workspace_id"),
            "state": state,
            "status": _agent_pbx_status(state),
            "cancel_reused": False,
            "cleanup": receipt["cleanup"],
        }

    def receipt(self, run_id: str) -> dict[str, Any]:
        """Return bounded redacted evidence without changing local journal state."""

        run_id = _identifier(run_id, "run_id")
        self._require_available()
        journal = self._read_journal(run_id)
        try:
            return self._validated_receipt(run_id, journal)
        except WorkerBeeError:
            raise
        except Exception as exc:  # receipt remains read-only
            raise _adapter_error("receipt", exc) from None

    def recover_incomplete(self) -> list[dict[str, Any]]:
        """Reconcile durable nonterminal journals after an MCP daemon restart."""

        if not self.available or not self.runs_root.is_dir():
            return []
        recovered: list[dict[str, Any]] = []
        self._ensure_state_root()
        with FileLock(self.lock_path, label="controlled runs"):
            for path in sorted(self.runs_root.glob("crun-*/journal.json")):
                journal = _read_json(path, MAX_RECEIPT_BYTES)
                state = str(journal.get("state") or "")
                if state not in CONTROLLED_RUN_ACTIVE_STATES:
                    continue
                run_id = _identifier(journal.get("run_id"), "run_id")
                try:
                    observed = _plain_mapping(self.adapter.recover(run_id=run_id, journal=journal))
                    recovered_state = _state(observed)
                    if (
                        recovered_state not in CONTROLLED_RUN_TERMINAL_STATES
                        and recovered_state != "recovery_required"
                    ):
                        raise _error(
                            "WORKERBEE_CONTROLLED_RUN_RECOVERY_INCOMPLETE",
                            "Strict adapter recovery did not reach cleanup or a terminal state.",
                        )
                    receipt = self._validated_receipt(run_id, journal)
                    cleanup = receipt["cleanup"]
                    if recovered_state in CONTROLLED_RUN_TERMINAL_STATES and (
                        not cleanup["complete"] or cleanup["residue"]
                    ):
                        recovered_state = "recovery_required"
                    journal = self._transition(
                        journal,
                        recovered_state,
                        adapter_result=_bounded_mapping(observed, MAX_EVIDENCE_BYTES),
                    )
                except Exception as exc:  # noqa: BLE001 - persist fail-closed recovery state
                    journal, cleanup = self._recover_after_adapter_failure(
                        journal,
                        exc,
                        operation="restart_recovery",
                    )
                    recovered_state = str(journal["state"])
                self._write_journal(run_id, journal)
                recovered.append(
                    {
                        "run_id": run_id,
                        "state": recovered_state,
                        "cleanup": cleanup,
                    }
                )
        return recovered

    def _request(self, payload: Mapping[str, Any]) -> ControlledRunRequest:
        self._require_available()
        raw = _plain_mapping(payload)
        _reject_forbidden_fields(raw)
        unexpected = sorted(set(raw) - _REQUEST_KEYS)
        missing = sorted(_REQUEST_KEYS - set(raw))
        if unexpected or missing:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_REQUEST_INVALID",
                "Controlled-run request fields do not match the Agent PBX contract.",
                details={"missing": missing, "unexpected": unexpected},
            )
        if len(_canonical_json(raw)) > MAX_REQUEST_BYTES:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_REQUEST_LIMIT",
                "Controlled-run request exceeds the maximum encoded size.",
            )
        profile = _plain_mapping(raw.get("profile"))
        request = ControlledRunRequest(
            request_id=_identifier(raw.get("request_id"), "request_id"),
            idempotency_key=_identifier(raw.get("idempotency_key"), "idempotency_key"),
            request_sha256=_sha256(raw.get("request_sha256"), "request_sha256"),
            root_operator_agent_id=_identifier(
                raw.get("root_operator_agent_id"), "root_operator_agent_id"
            ),
            target_fork_agent_id=_identifier(
                raw.get("target_fork_agent_id"), "target_fork_agent_id"
            ),
            campaign_id=_identifier(raw.get("campaign_id"), "campaign_id"),
            workspace_binding_id=_identifier(
                raw.get("workspace_binding_id"), "workspace_binding_id"
            ),
            profile=profile,
            profile_sha256=_sha256(raw.get("profile_sha256"), "profile_sha256"),
            baseline_commit=_git_oid(raw.get("baseline_commit"), "baseline_commit"),
            candidate_commit=_git_oid(raw.get("candidate_commit"), "candidate_commit"),
            recipe_id=_identifier(raw.get("recipe_id"), "recipe_id"),
            tool_manifest_sha256=_sha256(raw.get("tool_manifest_sha256"), "tool_manifest_sha256"),
            canonical_payload_sha256=_sha256_json(raw),
        )
        if (
            str(profile.get("sha256") or "") != request.profile_sha256
            or agent_pbx_profile_sha256(profile) != request.profile_sha256
        ):
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT",
                "The embedded profile does not match the canonical Agent PBX profile digest.",
            )
        return request

    def _validated_recipe_policy(
        self, request: ControlledRunRequest
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        profile = request.profile
        expected = {
            "enabled": True,
            "backend": "workerbee-standard-workspace",
            "required_authority": "trusted-workbench",
            "source_mode": "immutable-commit-export",
            "network_mode": "private-loopback",
            "external_egress": "deny",
            "credentials": "none",
            "cleanup_required": True,
            "recipe_id": request.recipe_id,
            "tool_manifest_sha256": request.tool_manifest_sha256,
        }
        drift = {
            key: {"expected": value, "actual": profile.get(key)}
            for key, value in expected.items()
            if profile.get(key) != value
        }
        if drift:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT",
                "Controlled-run profile violates the strict fixed policy.",
                details={"drift": drift},
            )
        recipes = self._recipes(strict=True)
        recipe = recipes.get(request.recipe_id)
        if recipe is None:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_RECIPE_DENIED",
                f"Recipe {request.recipe_id!r} is not registered by the strict adapter.",
            )
        recipe_tool_sha = _sha256(recipe.get("tool_manifest_sha256"), "recipe.tool_manifest_sha256")
        if recipe_tool_sha != request.tool_manifest_sha256:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_TOOL_MANIFEST_DRIFT",
                "The fixed recipe tool manifest does not match the Agent PBX request.",
            )
        allowed_profiles = recipe.get("profile_sha256")
        if allowed_profiles is not None and str(allowed_profiles) != request.profile_sha256:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT",
                "The fixed recipe does not allow this profile digest.",
            )
        limits = {
            "wall_seconds": _bounded_int(
                profile.get("max_seconds"), "profile.max_seconds", 30, MAX_WALL_SECONDS
            ),
            "memory_mib": _bounded_int(
                profile.get("max_memory_mib"), "profile.max_memory_mib", 128, MAX_MEMORY_MIB
            ),
            "output_bytes": _bounded_int(
                profile.get("max_output_bytes"),
                "profile.max_output_bytes",
                1024,
                MAX_OUTPUT_BYTES,
            ),
            "concurrency": _bounded_int(
                profile.get("max_concurrency"),
                "profile.max_concurrency",
                1,
                MAX_CONCURRENCY,
            ),
            "artifact_count": _bounded_int(
                recipe.get("max_artifacts", MAX_ARTIFACTS),
                "recipe.max_artifacts",
                1,
                MAX_ARTIFACTS,
            ),
            "artifact_bytes": _bounded_int(
                recipe.get("max_artifact_bytes", MAX_ARTIFACT_BYTES),
                "recipe.max_artifact_bytes",
                1024,
                MAX_ARTIFACT_BYTES,
            ),
            "receipt_bytes": _bounded_int(
                recipe.get("max_receipt_bytes", MAX_RECEIPT_BYTES),
                "recipe.max_receipt_bytes",
                4096,
                MAX_RECEIPT_BYTES,
            ),
        }
        return recipe, _policy(limits)

    def _validated_source_attestation(
        self,
        request: ControlledRunRequest,
        attestation: Mapping[str, Any],
    ) -> dict[str, Any]:
        source = _plain_mapping(attestation.get("source"))
        expected = {
            "mode": "immutable-commit-export",
            "export_count": 1,
            "baseline_commit": request.baseline_commit,
            "candidate_commit": request.candidate_commit,
            "caller_worktree_read_only": True,
            "source_roots_read_only": True,
        }
        drift = {
            key: {"expected": value, "actual": source.get(key)}
            for key, value in expected.items()
            if source.get(key) != value
        }
        for key in ("export_sha256", "baseline_tree", "candidate_tree"):
            value = source.get(key)
            matcher = _SHA256 if key == "export_sha256" else _GIT_OID
            if not matcher.fullmatch(str(value or "")):
                drift[key] = {"expected": matcher.pattern, "actual": value}
        if drift:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_SOURCE_DRIFT",
                "Strict adapter did not attest the requested immutable committed-tree export.",
                details={"drift": drift},
            )
        redacted = _redact(source)
        if len(_canonical_json(redacted)) > MAX_EVIDENCE_BYTES:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_SOURCE_LIMIT",
                "Controlled-run source attestation exceeds the bounded evidence limit.",
            )
        return redacted

    def _validated_receipt(
        self,
        run_id: str,
        journal: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            raw = _redact(_plain_mapping(self.adapter.receipt(run_id=run_id)))
        except Exception as exc:  # noqa: BLE001 - normalize private adapter failures
            raise _adapter_error("receipt", exc) from None
        cleanup_raw = _plain_mapping(raw.get("cleanup"))
        residue_raw = cleanup_raw.get("residue")
        if not isinstance(residue_raw, list):
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_RECEIPT_INVALID",
                "Controlled-run receipt cleanup.residue must be an array.",
            )
        residue = [str(item)[:1000] for item in residue_raw[:MAX_RESIDUE_ITEMS]]
        cleanup = {
            **cleanup_raw,
            "complete": cleanup_raw.get("complete") is True,
            "residue": residue,
        }
        evidence = _plain_mapping(raw.get("evidence"))
        limits = _plain_mapping(journal.get("limits") or {})
        _validate_artifact_bounds(evidence, limits)
        receipt_limit = min(
            int(limits.get("receipt_bytes") or MAX_RECEIPT_BYTES), MAX_RECEIPT_BYTES
        )
        evidence_limit = min(MAX_EVIDENCE_BYTES, max(4096, receipt_limit * 3 // 4))
        bounded_evidence = _bounded_mapping(evidence, evidence_limit)
        receipt = {
            "api_version": CONTROLLED_RUN_RECEIPT_VERSION,
            "run_id": run_id,
            "request_id": str(journal.get("request_id") or ""),
            "workspace_id": str(raw.get("workspace_id") or journal.get("workspace_id") or ""),
            "state": str(journal.get("state") or ""),
            "request_sha256": str(journal.get("request_sha256") or ""),
            "canonical_payload_sha256": str(journal.get("canonical_payload_sha256") or ""),
            "profile_sha256": str(journal.get("profile_sha256") or ""),
            "tool_manifest_sha256": str(journal.get("tool_manifest_sha256") or ""),
            "root_operator_agent_id": str(journal.get("root_operator_agent_id") or ""),
            "target_fork_agent_id": str(journal.get("target_fork_agent_id") or ""),
            "campaign_id": str(journal.get("campaign_id") or ""),
            "workspace_binding_id": str(journal.get("workspace_binding_id") or ""),
            "baseline_commit": str(journal.get("baseline_commit") or ""),
            "candidate_commit": str(journal.get("candidate_commit") or ""),
            "recipe_id": str(journal.get("recipe_id") or ""),
            "policy_sha256": str(journal.get("policy_sha256") or ""),
            "source": _redact(_plain_mapping(journal.get("source") or {})),
            "evidence": bounded_evidence,
            "cleanup": cleanup,
        }
        receipt["success"] = receipt["state"] == "succeeded" and cleanup["complete"] and not residue
        encoded = _canonical_json(receipt)
        if len(encoded) > receipt_limit:
            receipt["evidence"] = {
                "truncated": True,
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
            encoded = _canonical_json(receipt)
        if len(encoded) > receipt_limit:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_RECEIPT_LIMIT",
                "Controlled-run receipt exceeds the maximum safe size.",
            )
        receipt["receipt_sha256"] = hashlib.sha256(encoded).hexdigest()
        if len(_canonical_json(receipt)) > receipt_limit:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_RECEIPT_LIMIT",
                "Controlled-run receipt exceeds the maximum safe size.",
            )
        return receipt

    def _recover_after_start_failure(
        self,
        journal: dict[str, Any],
        exc: Exception,
    ) -> dict[str, Any]:
        recovered, _cleanup = self._recover_after_adapter_failure(
            journal,
            exc,
            operation="start",
            cancel_first=True,
        )
        return recovered

    def _recover_after_adapter_failure(
        self,
        journal: Mapping[str, Any],
        exc: Exception,
        *,
        operation: str,
        cancel_first: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Fail closed into a durable terminal or recovery-required journal."""

        run_id = str(journal["run_id"])
        evidence: dict[str, Any] = {
            "operation": operation,
            "error_type": type(exc).__name__,
        }
        cleanup: dict[str, Any] = {
            "complete": False,
            "residue": ["cleanup receipt unavailable"],
        }
        recovered_state = "recovery_required"
        if cancel_first:
            try:
                canceled = _plain_mapping(
                    self.adapter.cancel(
                        run_id=run_id,
                        reason=f"{operation} failed; mandatory cleanup",
                    )
                )
                evidence["cancel"] = _bounded_mapping(canceled, MAX_EVIDENCE_BYTES // 4)
            except Exception as cancel_exc:  # noqa: BLE001 - recovery must still be attempted
                evidence["cancel_error_type"] = type(cancel_exc).__name__
        recovery_state: str | None = None
        try:
            recovered = _plain_mapping(self.adapter.recover(run_id=run_id, journal=journal))
            evidence["recovery"] = _bounded_mapping(recovered, MAX_EVIDENCE_BYTES // 2)
            recovery_state = _state(recovered)
            if (
                recovery_state not in CONTROLLED_RUN_TERMINAL_STATES
                and recovery_state != "recovery_required"
            ):
                raise _error(
                    "WORKERBEE_CONTROLLED_RUN_RECOVERY_INCOMPLETE",
                    "Strict adapter recovery did not reach cleanup or a terminal state.",
                )
        except Exception as recovery_exc:  # noqa: BLE001 - persist only safe error type
            evidence["recovery_error_type"] = type(recovery_exc).__name__
            recovery_state = None
        try:
            receipt = self._validated_receipt(run_id, journal)
            evidence["receipt_observed"] = True
            if recovery_state is not None:
                cleanup = receipt["cleanup"]
            if (
                recovery_state in CONTROLLED_RUN_TERMINAL_STATES
                and cleanup["complete"]
                and not cleanup["residue"]
            ):
                recovered_state = "failed" if operation == "start" else recovery_state
        except Exception as receipt_exc:  # noqa: BLE001 - persist only safe error type
            evidence["receipt_error_type"] = type(receipt_exc).__name__
        evidence["cleanup"] = cleanup
        return (
            self._transition(
                journal,
                recovered_state,
                adapter_result=_bounded_mapping(evidence, MAX_EVIDENCE_BYTES),
            ),
            cleanup,
        )

    def _transition(
        self,
        journal: Mapping[str, Any],
        state: str,
        *,
        workspace_id: str | None = None,
        adapter_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in CONTROLLED_RUN_STATES:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_STATUS_INVALID",
                f"Strict adapter returned unsupported state {state!r}.",
            )
        updated = dict(journal)
        updated["state"] = state
        updated["updated_at"] = time.time()
        if workspace_id is not None:
            updated["workspace_id"] = workspace_id
        history = list(updated.get("history") or [])
        if not history or history[-1].get("state") != state:
            history.append({"state": state, "at": updated["updated_at"]})
        updated["history"] = history[-128:]
        if adapter_result is not None:
            updated["adapter_result"] = _redact(dict(adapter_result))
        return updated

    def _idempotent_record(self, request: ControlledRunRequest) -> dict[str, Any] | None:
        path = self._idempotency_path(request.idempotency_key)
        if not path.is_file():
            return None
        entry = _read_json(path, 16 * 1024)
        if (
            entry.get("request_sha256") != request.request_sha256
            or entry.get("canonical_payload_sha256") != request.canonical_payload_sha256
        ):
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_IDEMPOTENCY_CONFLICT",
                "The idempotency key is already bound to a different canonical request.",
            )
        return self._read_journal(_identifier(entry.get("run_id"), "run_id"))

    def _enforce_concurrency(self, policy: Mapping[str, Any]) -> None:
        active = 0
        if self.runs_root.is_dir():
            for path in self.runs_root.glob("crun-*/journal.json"):
                record = _read_json(path, MAX_RECEIPT_BYTES)
                if str(record.get("state") or "") in CONTROLLED_RUN_ACTIVE_STATES:
                    active += 1
        maximum = int(_plain_mapping(policy.get("limits")).get("concurrency") or 1)
        if active >= maximum:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_CONCURRENCY_LIMIT",
                f"Controlled-run concurrency limit {maximum} is already in use.",
                retryable=True,
            )

    def _recipes(self, *, strict: bool) -> dict[str, dict[str, Any]]:
        try:
            raw = self.adapter.recipes()
        except Exception as exc:
            if strict:
                raise _adapter_error("recipe discovery", exc) from None
            return {}
        if not isinstance(raw, Mapping):
            if strict:
                raise _error(
                    "WORKERBEE_CONTROLLED_RUN_ADAPTER_INVALID",
                    "Strict adapter recipes must be an object.",
                )
            return {}
        recipes: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            recipe_id = str(key or "")
            if _ID.fullmatch(recipe_id) and isinstance(value, Mapping):
                recipes[recipe_id] = dict(value)
        return recipes

    def _require_available(self) -> None:
        capabilities = self.capabilities()
        if self.mode != "enabled":
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_DISABLED",
                "WorkerBee controlled runs are disabled by local policy.",
            )
        if not capabilities["available"]:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_CAPABILITY_UNAVAILABLE",
                str(capabilities["reason"]),
            )

    def _ensure_state_root(self) -> None:
        _private_dir(self.root)
        _private_dir(self.runs_root)
        _private_dir(self.idempotency_root)

    def _run_dir(self, run_id: str) -> Path:
        return self.runs_root / _identifier(run_id, "run_id")

    def _journal_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "journal.json"

    def _idempotency_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.idempotency_root / f"{digest}.json"

    def _read_journal(self, run_id: str) -> dict[str, Any]:
        path = self._journal_path(run_id)
        if not path.is_file():
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_NOT_FOUND",
                f"Controlled run {run_id!r} was not found.",
            )
        return _read_json(path, MAX_RECEIPT_BYTES)

    def _write_journal(self, run_id: str, journal: Mapping[str, Any]) -> None:
        if len(_canonical_json(_redact(dict(journal)))) > MAX_RECEIPT_BYTES:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_STATE_LIMIT",
                "Controlled-run journal exceeds its maximum size.",
            )
        _atomic_private_json(self._journal_path(run_id), journal)

    def _write_idempotency(self, request: ControlledRunRequest, run_id: str) -> None:
        _atomic_private_json(
            self._idempotency_path(request.idempotency_key),
            {
                "run_id": run_id,
                "request_sha256": request.request_sha256,
                "canonical_payload_sha256": request.canonical_payload_sha256,
            },
        )

    @staticmethod
    def _start_response(journal: Mapping[str, Any], *, reused: bool) -> dict[str, Any]:
        return {
            "api_version": CONTROLLED_RUN_API_VERSION,
            "run_id": str(journal["run_id"]),
            "workspace_id": str(journal.get("workspace_id") or ""),
            "state": str(journal["state"]),
            "status": _agent_pbx_status(str(journal["state"])),
            "idempotent_reuse": reused,
        }


def agent_pbx_profile_sha256(profile: Mapping[str, Any]) -> str:
    """Reproduce Agent PBX's canonical controlled-run profile digest."""

    value = _plain_mapping(profile)
    missing = [key for key in _AGENT_PBX_PROFILE_DIGEST_KEYS if key not in value]
    if missing:
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT",
            "The embedded profile is missing canonical Agent PBX fields.",
            details={"missing": missing},
        )
    normalized = {key: value[key] for key in _AGENT_PBX_PROFILE_DIGEST_KEYS}
    for key in ("allowed_workerbee_backends", "allowed_projects"):
        items = normalized[key]
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT",
                f"The embedded profile field {key} is not a canonical string array.",
            )
        normalized[key] = list(items)
    for key in ("enabled", "cleanup_required", "allow_host_fallback"):
        if not isinstance(normalized[key], bool):
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT",
                f"The embedded profile field {key} is not canonical boolean data.",
            )
    for key in (
        "version",
        "max_seconds",
        "max_memory_mib",
        "max_output_bytes",
        "max_concurrency",
        "lease_max_uses",
        "lease_ttl_seconds",
        "retention_seconds",
    ):
        if not isinstance(normalized[key], int) or isinstance(normalized[key], bool):
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT",
                f"The embedded profile field {key} is not canonical integer data.",
            )
    return _sha256_json(normalized)


def _validate_artifact_bounds(evidence: Mapping[str, Any], limits: Mapping[str, Any]) -> None:
    artifacts = evidence.get("artifacts")
    if artifacts is None:
        return
    if not isinstance(artifacts, list):
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_RECEIPT_INVALID",
            "Controlled-run evidence.artifacts must be an array.",
        )
    maximum_count = min(int(limits.get("artifact_count") or MAX_ARTIFACTS), MAX_ARTIFACTS)
    if len(artifacts) > maximum_count:
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_ARTIFACT_LIMIT",
            "Controlled-run artifact count exceeds the fixed recipe limit.",
        )
    maximum_bytes = min(int(limits.get("artifact_bytes") or MAX_ARTIFACT_BYTES), MAX_ARTIFACT_BYTES)
    total_bytes = 0
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_RECEIPT_INVALID",
                "Controlled-run artifact entries must be objects.",
            )
        size = artifact.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_RECEIPT_INVALID",
                "Controlled-run artifact size_bytes must be a non-negative integer.",
            )
        total_bytes += size
        if total_bytes > maximum_bytes:
            raise _error(
                "WORKERBEE_CONTROLLED_RUN_ARTIFACT_LIMIT",
                "Controlled-run artifact bytes exceed the fixed recipe limit.",
            )


def _policy(limits: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": {
            "mode": "immutable-commit-export",
            "combined_export_count": 1,
            "caller_worktree": "read-only",
            "source_roots": "read-only",
        },
        "network": {
            "loopback_tcp": "allow",
            "pathname_af_unix": "allow",
            "dns": "deny",
            "non_loopback_interfaces": "deny",
            "non_loopback_routes": "deny",
            "non_loopback_connects": "deny",
            "external_egress": "deny",
            "abstract_af_unix": "deny",
            "host_control_sockets": "deny",
        },
        "environment": {
            "inherit": False,
            "ambient_credentials": "deny",
            "ambient_config": "deny",
            "caller_values": "deny",
        },
        "tools": {
            "fixed_recipe_only": True,
            "pinned_manifest_required": True,
            "package_install": "deny",
            "package_index": "deny",
            "fallback": "deny",
        },
        "filesystem": {
            "run_root_mode": "0700",
            "disposable_scratch": True,
            "host_repository_writes": "deny",
        },
        "cleanup": {
            "always": True,
            "residue_prevents_success": True,
            "crash_recovery": "required",
        },
        "limits": dict(limits),
    }


def _state(payload: Mapping[str, Any]) -> str:
    state = str(payload.get("state") or payload.get("status") or "").strip().lower()
    if state not in CONTROLLED_RUN_STATES:
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_STATUS_INVALID",
            f"Strict adapter returned unsupported state {state!r}.",
        )
    return state


def _agent_pbx_status(state: str) -> str:
    """Bridge required lifecycle names to the current Agent PBX executor vocabulary."""

    return _AGENT_PBX_STATUS_ALIASES.get(state, state)


def _adapter_error(operation: str, exc: Exception) -> WorkerBeeError:
    """Return a stable public error without private adapter message content."""

    timeout = isinstance(exc, TimeoutError)
    return _error(
        (
            "WORKERBEE_CONTROLLED_RUN_ADAPTER_TIMEOUT"
            if timeout
            else "WORKERBEE_CONTROLLED_RUN_ADAPTER_ERROR"
        ),
        f"Strict controlled-run adapter {operation} failed.",
        details={"operation": operation, "error_type": type(exc).__name__},
        retryable=timeout,
        remediation=(
            "Inspect the private adapter journal and complete bounded recovery before retrying."
        ),
    )


def _reject_forbidden_fields(value: Any, *, path: str = "request") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_REQUEST_KEYS:
                raise _error(
                    "WORKERBEE_CONTROLLED_RUN_CALLER_COMMAND_DENIED",
                    f"{path}.{key} is forbidden; the fixed recipe owns commands and environment.",
                )
            _reject_forbidden_fields(child, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _reject_forbidden_fields(child, path=f"{path}[{index}]")


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    if not path.is_dir() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_ROOT_UNSAFE",
            f"Controlled-run path is not an owner-private directory: {path}",
        )


def _atomic_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    _private_dir(path.parent)
    encoded = json.dumps(
        _redact(dict(payload)), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path, maximum: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_STATE_INVALID",
            f"Controlled-run state file is missing or unsafe: {path}",
        )
    if path.stat().st_size > maximum:
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_STATE_LIMIT",
            f"Controlled-run state file exceeds its maximum size: {path}",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_STATE_INVALID",
            f"Controlled-run state is unreadable: {path}",
        ) from exc
    return _plain_mapping(value)


def _bounded_mapping(value: Mapping[str, Any], maximum: int) -> dict[str, Any]:
    redacted = _redact(dict(value))
    encoded = _canonical_json(redacted)
    if len(encoded) <= maximum:
        return redacted
    return {
        "truncated": True,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(child) for child in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value[:16_384]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:16_384]


def _plain_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_CONTRACT_INVALID",
            "Controlled-run contract value must be an object.",
        )
    return dict(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _identifier(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not _ID.fullmatch(normalized):
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_REQUEST_INVALID",
            f"{name} must be a safe 1 to 240 character identifier.",
        )
    return normalized


def _git_oid(value: Any, name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _GIT_OID.fullmatch(normalized):
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_SOURCE_INVALID",
            f"{name} must be a full 40 to 64 character lowercase Git object ID.",
        )
    return normalized


def _sha256(value: Any, name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256.fullmatch(normalized):
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_REQUEST_INVALID",
            f"{name} must be a lowercase SHA-256 digest.",
        )
    return normalized


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_PROFILE_INVALID",
            f"{name} must be an integer.",
        ) from exc
    if result < minimum or result > maximum:
        raise _error(
            "WORKERBEE_CONTROLLED_RUN_PROFILE_INVALID",
            f"{name} must be between {minimum} and {maximum}.",
        )
    return result


def _error(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    retryable: bool = False,
    remediation: str | None = None,
) -> WorkerBeeError:
    return WorkerBeeError(
        code=code,
        message=message,
        details=_redact(dict(details or {})),
        retryable=retryable,
        remediation=remediation,
    )
