"""Stable WorkerBee v1 result envelopes and errors."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

API_VERSION = "workerbee.mcp/v1"
AGENT_FEEDBACK_SCHEMA = "workerbee.agent_feedback/v1"
AGENT_FEEDBACK_LIMIT = 6
MCP_TOOL_NAMES = [
    "workerbee_v1_capabilities",
    "workerbee_v1_session_start",
    "workerbee_v1_projects",
    "workerbee_v1_project_mode_get",
    "workerbee_v1_project_mode_set",
    "workerbee_v1_project_start",
    "workerbee_v1_project_status",
    "workerbee_v1_project_stop",
    "workerbee_v1_project_reset",
    "workerbee_v1_profile_list",
    "workerbee_v1_profile_start",
    "workerbee_v1_profile_status",
    "workerbee_v1_profile_stop",
    "workerbee_v1_profile_validate",
    "workerbee_v1_profile_workload_status",
    "workerbee_v1_profile_workload_validate",
    "workerbee_v1_logs",
    "workerbee_v1_exec",
    "workerbee_v1_ingress_status",
    "workerbee_v1_ingress_probe",
    "workerbee_v1_image_build",
    "workerbee_v1_manifest_prepare",
    "workerbee_v1_manifest_validate",
    "workerbee_v1_manifest_deploy_local",
    "workerbee_v1_manifest_deploy_remote_k1s",
    "workerbee_v1_bundle_export",
    "workerbee_v1_secret_policy_status",
    "workerbee_v1_security_assess",
    "workerbee_v1_security_review_project",
    "workerbee_v1_cleanup",
    "workerbee_v1_trust_status",
    "workerbee_v1_trust_install",
    "workerbee_v1_trust_uninstall",
]


@dataclass(slots=True)
class WorkerBeeError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    remediation: str | None = None

    def __str__(self) -> str:
        return self.message

    def public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": _mask_sensitive(self.details),
            "retryable": self.retryable,
            "remediation": self.remediation,
        }


def ok(
    *,
    kind: str,
    data: dict[str, Any] | None = None,
    project: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    data_payload = _jsonable(data or {})
    return _with_agent_feedback(
        {
            "api_version": API_VERSION,
            "kind": kind,
            "ok": True,
            "project": project,
            "data": data_payload,
            "warnings": warnings or [],
            "error": None,
        }
    )


def fail(
    exc: Exception,
    *,
    kind: str,
    project: str | None = None,
) -> dict[str, Any]:
    if isinstance(exc, WorkerBeeError):
        error = exc
    elif isinstance(exc, FileNotFoundError):
        error = WorkerBeeError(
            code="NOT_FOUND",
            message=str(exc),
            remediation="Check the path and retry.",
        )
    elif isinstance(exc, PermissionError):
        error = WorkerBeeError(
            code="PERMISSION_DENIED",
            message=str(exc),
            remediation="Check filesystem permissions and retry.",
        )
    elif isinstance(exc, TimeoutError):
        error = WorkerBeeError(
            code="TIMEOUT",
            message=str(exc),
            retryable=True,
            remediation="Retry after checking runtime and service health.",
        )
    elif isinstance(exc, ValueError):
        error = WorkerBeeError(
            code="VALIDATION_FAILED",
            message=str(exc),
            remediation="Review the input and run validation again.",
        )
    else:
        error = WorkerBeeError(
            code="INTERNAL_ERROR",
            message=str(exc),
            remediation="Inspect WorkerBee logs or rerun with a narrower operation.",
        )
    return _with_agent_feedback(
        {
            "api_version": API_VERSION,
            "kind": kind,
            "ok": False,
            "project": project,
            "data": {},
            "warnings": [],
            "error": error.public_dict(),
        }
    )


def protect(kind: str, project: str | None, fn) -> dict[str, Any]:  # noqa: ANN001
    try:
        data = fn()
        payload = data if isinstance(data, dict) else {"value": data}
        return ok(kind=kind, data=payload, project=project)
    except Exception as exc:  # noqa: BLE001
        return fail(exc, kind=kind, project=project)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    if hasattr(value, "public_dict"):
        return _jsonable(value.public_dict())
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return value


def _with_agent_feedback(envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("agent_feedback"), dict):
        data = dict(data)
        data["agent_feedback"] = _agent_feedback_for_envelope(envelope, data)
        envelope["data"] = data
    return envelope


def _agent_feedback_for_envelope(
    envelope: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    ok_value = bool(envelope.get("ok"))
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else None
    warnings = envelope.get("warnings") if isinstance(envelope.get("warnings"), list) else []
    severity = _feedback_severity(ok=ok_value, data=data, warnings=warnings)
    feedback = {
        "schema": AGENT_FEEDBACK_SCHEMA,
        "severity": severity,
        "summary": _feedback_summary(
            kind=str(envelope.get("kind") or "Result"),
            ok=ok_value,
            project=_feedback_project(envelope.get("project"), data),
            data=data,
            error=error,
        ),
        "observations": _limit(_feedback_observations(data=data, error=error, warnings=warnings)),
        "next_actions": _limit(
            _feedback_next_actions(
                kind=str(envelope.get("kind") or "Result"),
                ok=ok_value,
                project=_feedback_project(envelope.get("project"), data),
                data=data,
                error=error,
            )
        ),
        "links": _limit(_feedback_links(data)),
        "artifacts": _limit(_feedback_artifacts(data)),
    }
    return _jsonable(_mask_sensitive(feedback))


def _feedback_severity(*, ok: bool, data: dict[str, Any], warnings: list[Any]) -> str:
    if not ok:
        return "error"
    if _data_reports_problem(data) or warnings:
        return "warning"
    if _data_reports_success(data):
        return "ok"
    return "info"


def _feedback_summary(
    *,
    kind: str,
    ok: bool,
    project: str | None,
    data: dict[str, Any],
    error: dict[str, Any] | None,
) -> str:
    label = _kind_label(kind)
    project_text = f" for `{project}`" if project else ""
    if not ok:
        code = str((error or {}).get("code") or "ERROR")
        if code == "EXEC_COMMAND_FAILED":
            details = error.get("details") if isinstance(error, dict) else {}
            if isinstance(details, dict):
                output = str(details.get("stderr") or details.get("stdout") or "").strip()
                first_line = next(
                    (line.strip() for line in output.splitlines() if line.strip()),
                    "",
                )
                safe_output = _safe_summary_text(first_line) if first_line else None
                if safe_output:
                    return f"{label}{project_text} failed with {code}: {safe_output}"
        remediation = str((error or {}).get("remediation") or "").strip()
        safe_remediation = _safe_summary_text(remediation) if remediation else None
        suffix = f" {safe_remediation}" if safe_remediation else ""
        return f"{label}{project_text} failed with {code}.{suffix}"

    explicit = data.get("user_message")
    if isinstance(explicit, str) and explicit.strip():
        safe_explicit = _safe_summary_text(explicit.strip())
        if safe_explicit:
            return safe_explicit

    if kind == "SessionStart":
        mode = str(data.get("mode") or "lazy")
        running = _running_state(data)
        selected_project = project or data.get("project") or "default"
        if running is True:
            return f"WorkerBee project `{selected_project}` is running."
        if mode == "stop":
            return f"WorkerBee project `{selected_project}` is stopped by mode."
        return f"WorkerBee project `{selected_project}` is registered in {mode} mode."

    if kind in {"ProjectStatus", "ProjectModeGet", "ProjectModeSet", "ProjectStart"}:
        running = _running_state(data)
        selected_project = project or data.get("project") or "default"
        app_status = _app_status(data)
        if running is True and app_status.get("state") == "no_workload_deployed":
            return (
                f"WorkerBee project `{selected_project}` control plane is running, "
                "but no app workload is deployed."
            )
        if app_status.get("state") == "degraded":
            return f"WorkerBee project `{selected_project}` has degraded app workloads."
        if app_status.get("state") == "orphaned":
            return f"WorkerBee project `{selected_project}` has orphaned app workloads."
        if running is True:
            return f"WorkerBee project `{selected_project}` is running."
        if running is False:
            mode = str(data.get("mode") or _nested_get(data, "project_status", "mode") or "lazy")
            return f"WorkerBee project `{selected_project}` is not running and is in {mode} mode."

    if kind == "Projects":
        projects = data.get("projects") if isinstance(data.get("projects"), list) else []
        running = sum(1 for item in projects if isinstance(item, dict) and item.get("running"))
        return f"WorkerBee knows {len(projects)} project(s); {running} are running."

    if kind == "Logs":
        app = data.get("resolved_app") or data.get("app")
        source = data.get("source") or "runtime"
        return f"Fetched recent {source} logs{f' for `{app}`' if app else ''}."

    if kind == "IngressProbe":
        status = data.get("status")
        url = _safe_url(data.get("url"))
        if data.get("ok") is False:
            return f"Ingress probe did not match expectations{f' for {url}' if url else ''}."
        return f"Ingress probe succeeded{f' with HTTP {status}' if status else ''}."

    if kind in {"ManifestValidate", "ManifestDeployLocal", "ManifestDeployRemoteK1s"}:
        app_status = _app_status(data)
        if app_status.get("state") == "degraded":
            return f"{label}{project_text} completed but app workloads are degraded."
        if app_status.get("state") == "orphaned":
            return f"{label}{project_text} completed with orphaned workloads."
        if data.get("ready") is False or _nested_get(data, "wait", "ready") is False:
            return f"{label}{project_text} completed but workloads are not ready."
        return f"{label}{project_text} completed."

    if kind == "BundleExport":
        fmt = data.get("format")
        return f"Exported WorkerBee artifacts{f' as {fmt}' if fmt else ''}."

    if kind == "SecurityReviewProject":
        report = data.get("report") if isinstance(data.get("report"), dict) else {}
        report_path = _safe_path(str(report.get("path") or ""))
        suffix = f" and wrote {report_path}" if report_path else ""
        return f"Security review completed{suffix}."

    if _data_reports_problem(data):
        return f"{label}{project_text} completed with follow-up needed."
    return f"{label}{project_text} completed."


def _feedback_observations(
    *,
    data: dict[str, Any],
    error: dict[str, Any] | None,
    warnings: list[Any],
) -> list[str]:
    observations: list[str] = []
    if error:
        code = error.get("code")
        if code:
            observations.append(f"error code: {code}")
        retryable = error.get("retryable")
        if retryable:
            observations.append("retryable: true")
    if warnings:
        observations.append(f"warnings: {len(warnings)}")

    mode = data.get("mode") or _nested_get(data, "project_status", "mode")
    if mode:
        observations.append(f"mode: {mode}")
    running = _running_state(data)
    if running is not None:
        observations.append(f"running: {str(running).lower()}")
    ready = (
        data.get("ready")
        if isinstance(data.get("ready"), bool)
        else _nested_get(data, "wait", "ready")
    )
    if isinstance(ready, bool):
        observations.append(f"ready: {str(ready).lower()}")
    app_status = _app_status(data)
    if app_status:
        state = app_status.get("state")
        if state:
            observations.append(f"app: {state}")
        declared = app_status.get("declared_workload_count")
        degraded = app_status.get("degraded_workload_count")
        orphaned = app_status.get("orphaned_workload_count")
        if any(isinstance(value, int) and value for value in (declared, degraded, orphaned)):
            observations.append(
                f"workloads: declared={int(declared or 0)}, "
                f"degraded={int(degraded or 0)}, orphaned={int(orphaned or 0)}"
            )
    status = data.get("status")
    if isinstance(status, int):
        observations.append(f"http status: {status}")
    elapsed_ms = data.get("elapsed_ms")
    if isinstance(elapsed_ms, int):
        observations.append(f"elapsed: {elapsed_ms} ms")
    latest = data.get("latest_deployment")
    if isinstance(latest, dict) and latest.get("id"):
        observations.append(f"latest deployment: {latest['id']}")
    deployment = data.get("deployment")
    if isinstance(deployment, dict) and deployment.get("id"):
        observations.append(f"deployment: {deployment['id']}")
    report = data.get("report")
    if isinstance(report, dict) and report.get("id"):
        observations.append(f"report: {report['id']}")
    return _dedupe(observations)


def _feedback_next_actions(
    *,
    kind: str,
    ok: bool,
    project: str | None,
    data: dict[str, Any],
    error: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    project_args = {"project": project} if project else {}

    if not ok:
        code = str((error or {}).get("code") or "")
        if code == "PROJECT_STOPPED":
            actions.append(
                _action(
                    "workerbee_v1_project_mode_set",
                    {**project_args, "mode": "start"},
                    "re-enable this WorkerBee project",
                )
            )
        elif code == "STACK_NOT_RUNNING":
            actions.append(
                _action(
                    "workerbee_v1_project_start",
                    project_args,
                    "start the project stack",
                )
            )
        elif code in {"INGRESS_NOT_READY", "CA_NOT_READY", "PROBE_FAILED", "TIMEOUT"}:
            actions.append(
                _action(
                    "workerbee_v1_project_status",
                    project_args,
                    "check runtime and ingress state",
                )
            )
            actions.append(_action("workerbee_v1_logs", project_args, "inspect recent app logs"))
        else:
            actions.append(
                _action(
                    "workerbee_v1_project_status",
                    project_args,
                    "refresh current project state",
                )
            )
        return actions

    stage = _stage_ref(data)
    if kind == "ManifestPrepare" and stage:
        actions.append(
            _action(
                "workerbee_v1_manifest_validate",
                {**project_args, "stage": stage},
                "validate the staged manifests",
            )
        )
    elif kind == "ManifestValidate" and data.get("ok") is not False and stage:
        actions.append(
            _action(
                "workerbee_v1_manifest_deploy_local",
                {**project_args, "stage": stage},
                "deploy the validated stage locally",
            )
        )
    elif kind in {"ManifestDeployLocal", "ManifestDeployRemoteK1s"}:
        actions.append(
            _action("workerbee_v1_project_status", project_args, "verify workload status")
        )
        app_status = _app_status(data)
        if kind == "ManifestDeployLocal" and stage and app_status.get("orphaned_workload_count"):
            actions.append(
                _action(
                    "workerbee_v1_manifest_deploy_local",
                    {**project_args, "stage": stage, "prune": True},
                    "remove workloads from the previous stage that are absent now",
                )
            )
        if _feedback_links(data):
            actions.append(
                _action(
                    "workerbee_v1_ingress_probe",
                    project_args,
                    "smoke-test the exposed HTTPS ingress",
                )
            )
        if _data_reports_problem(data):
            actions.append(
                _action(
                    "workerbee_v1_logs",
                    project_args,
                    "inspect readiness or runtime failures",
                )
            )
    elif kind in {"SessionStart", "ProjectModeGet", "ProjectStatus"}:
        running = _running_state(data)
        mode = str(data.get("mode") or _nested_get(data, "project_status", "mode") or "")
        if mode == "stop":
            actions.append(
                _action(
                    "workerbee_v1_project_mode_set",
                    {**project_args, "mode": "start"},
                    "re-enable WorkerBee for runtime validation",
                )
            )
        elif running is False:
            actions.append(
                _action(
                    "workerbee_v1_project_start",
                    project_args,
                    "start the project when runtime validation is needed",
                )
            )
        else:
            actions.append(_action("workerbee_v1_logs", project_args, "inspect recent app logs"))
            if _feedback_links(data):
                actions.append(
                    _action(
                        "workerbee_v1_ingress_probe",
                        project_args,
                        "probe a known WorkerBee HTTPS URL",
                    )
                )
    elif kind == "IngressProbe" and data.get("ok") is False:
        actions.append(
            _action(
                "workerbee_v1_project_status",
                project_args,
                "check ingress and workload state",
            )
        )
        actions.append(
            _action("workerbee_v1_logs", project_args, "inspect the app behind the route")
        )
    elif kind == "Logs":
        actions.append(
            _action(
                "workerbee_v1_project_status",
                project_args,
                "correlate logs with workload status",
            )
        )
    elif kind == "SecurityReviewProject":
        report = data.get("report") if isinstance(data.get("report"), dict) else {}
        if report.get("path"):
            actions.append(
                _action(
                    "workerbee_v1_security_review_project",
                    project_args,
                    "rerun after fixes to refresh the report",
                )
            )
    return actions


def _action(tool: str, args: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "tool": tool,
        "args": {key: value for key, value in args.items() if value not in (None, "")},
        "reason": reason,
    }


def _feedback_links(data: Any) -> list[str]:
    urls: list[str] = []

    def add(value: Any) -> None:
        safe = _safe_url(value)
        if safe:
            urls.append(safe)

    def visit(value: Any, key: str = "") -> None:
        if _sensitive_key(key):
            return
        if isinstance(value, dict):
            if "agent_feedback" in value:
                value = {k: v for k, v in value.items() if k != "agent_feedback"}
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, list | tuple):
            for item in value:
                visit(item, key)
        elif isinstance(value, str):
            add(value)

    if isinstance(data, dict):
        for explicit in _explicit_ingress_link_values(data):
            add(explicit)
    visit(data)
    return _dedupe(urls)


def _explicit_ingress_link_values(data: dict[str, Any]) -> list[Any]:
    values: list[Any] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if "ingress_urls" in value:
                collect(value["ingress_urls"])
            for key in ("app_status", "deployment", "latest_deployment", "project_status"):
                if key in value:
                    collect(value[key])
        elif isinstance(value, list | tuple):
            for item in value:
                collect(item)
        else:
            values.append(value)

    collect(data)
    return values


def _feedback_artifacts(data: Any) -> list[str]:
    paths: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if _sensitive_key(key) or key in {"ca_bundle", "server_cert"}:
            return
        if isinstance(value, dict):
            if "agent_feedback" in value:
                value = {k: v for k, v in value.items() if k != "agent_feedback"}
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, list | tuple):
            for item in value:
                visit(item, key)
        elif isinstance(value, str) and _artifact_key(key):
            safe = _safe_path(value)
            if safe:
                paths.append(safe)

    visit(data)
    return _dedupe(paths)


def _artifact_key(key: str) -> bool:
    normalized = key.lower()
    return normalized in {
        "path",
        "stage_dir",
        "state_dir",
        "output_dir",
    } or normalized.endswith(("_path", "_dir"))


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def _safe_path(value: str) -> str | None:
    if not value or len(value) > 4096:
        return None
    if value.startswith(("http://", "https://")):
        return None
    if any(marker in value.lower() for marker in ("token=", "password=", "secret=")):
        return None
    return value


def _feedback_project(raw_project: Any, data: dict[str, Any]) -> str | None:
    if isinstance(raw_project, str) and raw_project:
        return raw_project
    raw_data_project = data.get("project")
    if isinstance(raw_data_project, str) and raw_data_project:
        return raw_data_project
    status = data.get("project_status")
    if isinstance(status, dict):
        raw_nested = status.get("project")
        if isinstance(raw_nested, str) and raw_nested:
            return raw_nested
    return None


def _running_state(data: dict[str, Any]) -> bool | None:
    if isinstance(data.get("running"), bool):
        return bool(data["running"])
    status = data.get("project_status")
    if isinstance(status, dict) and isinstance(status.get("running"), bool):
        return bool(status["running"])
    stack_running = data.get("stack_running")
    profile_running = data.get("profile_running")
    if isinstance(stack_running, bool) or isinstance(profile_running, bool):
        return bool(stack_running) or bool(profile_running)
    return None


def _data_reports_problem(data: dict[str, Any]) -> bool:
    if data.get("ok") is False:
        return True
    if data.get("ready") is False:
        return True
    wait = data.get("wait")
    if isinstance(wait, dict) and wait.get("ready") is False:
        return True
    app_status = _app_status(data)
    if app_status.get("degraded_workload_count") or app_status.get("orphaned_workload_count"):
        return True
    status_matches = data.get("status_matches")
    body_matches = data.get("body_matches")
    return status_matches is False or body_matches is False


def _data_reports_success(data: dict[str, Any]) -> bool:
    if data.get("ok") is True:
        return True
    if data.get("ready") is True:
        return True
    app_status = _app_status(data)
    if app_status.get("state") == "ready":
        return True
    running = _running_state(data)
    return running is True


def _app_status(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("app_status")
    if isinstance(value, dict):
        return value
    nested = _nested_get(data, "project_status", "app_status")
    return nested if isinstance(nested, dict) else {}


def _stage_ref(data: dict[str, Any]) -> str | None:
    for key in ("stage", "stage_dir"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    deployment = data.get("deployment")
    if isinstance(deployment, dict):
        value = deployment.get("stage_dir") or deployment.get("stage")
        if isinstance(value, str) and value:
            return value
    return None


def _nested_get(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _kind_label(kind: str) -> str:
    words: list[str] = []
    current = ""
    for char in kind:
        if char.isupper() and current:
            words.append(current)
            current = char
        else:
            current += char
    if current:
        words.append(current)
    return " ".join(words) or kind


def _clip_sentence(value: str) -> str:
    value = " ".join(value.split())
    if len(value) <= 220:
        return value
    return f"{value[:217].rstrip()}..."


def _safe_summary_text(value: str) -> str | None:
    if any(marker in value.lower() for marker in ("token=", "password=", "secret=")):
        return None
    return _clip_sentence(value)


def _limit(items: list[Any]) -> list[Any]:
    return items[:AGENT_FEEDBACK_LIMIT]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _sensitive_key(key: str) -> bool:
    return any(marker in key.lower() for marker in ("token", "password", "secret", "key"))


def _mask_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            key_str = str(key)
            if any(marker in key_str.lower() for marker in ("token", "password", "secret", "key")):
                out[key_str] = "***"
            else:
                out[key_str] = _mask_sensitive(val)
        return out
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return value
        if isinstance(parsed, dict):
            return json.dumps(_mask_sensitive(parsed), sort_keys=True)
    return value
