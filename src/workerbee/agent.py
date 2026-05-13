"""Agent-facing WorkerBee session policy and runbook helpers."""

from __future__ import annotations

import hashlib
import subprocess
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError
from workerbee.supervisor import project_slug

PROJECT_MODES = {"start", "lazy", "stop"}
DEFAULT_PROJECT_MODE = "lazy"
AGENT_INSTRUCTIONS_START = "<!-- workerbee-agent-instructions:v1 start -->"
AGENT_INSTRUCTIONS_END = "<!-- workerbee-agent-instructions:v1 end -->"


@dataclass(frozen=True, slots=True)
class SessionProjectInfo:
    project: str
    cwd: Path
    git_root: Path | None
    git_branch: str | None
    explicit_project: bool

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cwd"] = str(self.cwd)
        data["git_root"] = str(self.git_root) if self.git_root else None
        return data


def derive_session_project(cwd: Path | str, project: str | None = None) -> str:
    """Return a stable project id for a local agent checkout."""
    return derive_session_project_info(cwd, project=project).project


def derive_session_project_info(cwd: Path | str, project: str | None = None) -> SessionProjectInfo:
    """Return a stable project id plus Git metadata for a local agent checkout."""
    root = Path(cwd).expanduser().resolve()
    git_root = _git_root(root)
    git_branch = _git_branch(root) if git_root else None
    if project:
        return SessionProjectInfo(
            project=project_slug(project),
            cwd=root,
            git_root=git_root,
            git_branch=git_branch,
            explicit_project=True,
        )
    base_root = git_root or root
    base = project_slug(base_root.name or root.name or "workspace")
    if git_branch:
        base = project_slug(f"{base}-{git_branch}")
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:10]
    return SessionProjectInfo(
        project=project_slug(f"{base}-{digest}"),
        cwd=root,
        git_root=git_root,
        git_branch=git_branch,
        explicit_project=False,
    )


def _git_root(cwd: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        return None
    raw = proc.stdout.strip()
    if proc.returncode != 0 or not raw:
        return None
    return Path(raw).resolve()


def _git_branch(cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "symbolic-ref", "--quiet", "--short", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        return None
    raw = proc.stdout.strip()
    if proc.returncode != 0 or not raw or raw == "HEAD":
        return None
    return project_slug(raw)


def normalize_project_mode(mode: str | None) -> str:
    value = (mode or DEFAULT_PROJECT_MODE).strip().lower()
    if value not in PROJECT_MODES:
        raise WorkerBeeError(
            code="INVALID_PROJECT_MODE",
            message="project mode must be one of: start, lazy, stop",
            details={"mode": mode, "allowed": sorted(PROJECT_MODES)},
            remediation="Use `workerbee project mode start|lazy|stop`.",
        )
    return value


def runbook_markdown() -> str:
    return """# WorkerBee Cloud-Native Loop

Use WorkerBee when the task involves containers, services, manifests, ingress, databases,
queues, or integration behavior that benefits from a running local stack.

1. Call `workerbee_v1_session_start` with the absolute repo cwd and task goal.
2. Use the returned `project` value on every WorkerBee MCP tool call.
3. Use the local shell for repo edits, ordinary build scripts, unit tests, and temporary helper
   scripts. Put one-off helper scripts in `/tmp` or WorkerBee state unless the task requires a
   committed repo script.
4. Use WorkerBee MCP to build local images, prepare or stage manifests, validate manifests, deploy
   locally, inspect status/logs, probe HTTPS ingress, and export k1s/Kubernetes/Helm artifacts.
   For repo-root image builds with nested Dockerfiles, pass `dockerfile="path/to/Dockerfile"`.
5. When the user asks to bring, run, or start the project up in WorkerBee, treat that as a request
   for a running app workload: build needed images, stage and validate manifests, deploy with
   `workerbee_v1_manifest_deploy_local`, then inspect status/logs and probe ingress. Do not stop
   after `workerbee_v1_project_start` if deployable manifests or Containerfiles/Dockerfiles exist.
6. If this is the first WorkerBee run for a repo, there may be no deployed workload to inspect yet.
   Prefer existing repo manifests and Containerfiles/Dockerfiles. When they are absent, create a
   temporary native k1s staged deployment in WorkerBee state, then build, validate, and deploy it
   before runtime validation or security review.
7. When the user asks for a security review, call `workerbee_v1_security_review_project` after
   session bootstrap and project status. If it reports no deployment, stage and deploy the app
   first, then rerun the review.
8. In lazy mode, do not start the stack until deployment or an explicit project start is needed.
9. If WorkerBee reports `PROJECT_STOPPED`, tell the user WorkerBee is disabled for this project
   and show `workerbee project mode start --project <project>`.
10. Iterate against the live app through status, logs, exec, and `workerbee_v1_ingress_probe` until
   the requested behavior is verified. Use probe `headers` for signed requests such as S3 PUTs.
11. Export artifacts with `workerbee_v1_bundle_export` when the implementation is ready to hand off.

For staged WorkerBee manifests, app logs and exec default to the WorkerBee project namespace.
Use `app="namespace/name"` or pass `namespace` only when inspecting a non-default namespace;
do not guess generated runtime container names. Manifest validate/deploy/export accepts either the
absolute `stage_dir` returned by prepare or the named stage under project `artifacts/staged`.

For k1s controller/runtime development, prefer one agent launched from the WorkerBee repo
root and work across both checkouts: WorkerBee code in the current repo and k1s code in
the sibling `../k1s` checkout. Use an explicit direct-containerd WorkerBee project such as
`k1s-dev`, pass `k1s_root="../k1s"` or set `WORKERBEE_K1S_ROOT`, start a profile with
`workerbee_v1_profile_start`, deploy staged manifests with
`workerbee_v1_manifest_deploy_local(target="profile")`, inspect with
`workerbee_v1_profile_workload_status` and `workerbee_v1_logs(target="profile")`, and use
`workerbee_v1_profile_workload_validate` for the bundled realtime frontend/backend/db smoke test.
Use separate agents only for separable k1s/WorkerBee work, and isolate shared WorkerBee runtime
state with distinct `project` values unless one agent owns the shared profile lifecycle.
"""


def agent_instructions_markdown() -> str:
    """Return the canonical AGENTS.md WorkerBee instruction block."""
    return f"""{AGENT_INSTRUCTIONS_START}
## WorkerBee

When a task involves containers, services, manifests, ingress, databases,
queues, security review, or integration behavior, call WorkerBee MCP
`workerbee_v1_session_start` with the absolute repo cwd and task goal. Use the
returned `project` for every WorkerBee tool call.

Use the local shell for repo edits and ordinary tests. Use WorkerBee MCP for
local image builds, native k1s/Kubernetes manifest staging, validation,
deployment, status, logs, HTTPS ingress probes, security review, dashboard URLs,
cleanup, and artifact export.

If the user asks to bring, run, or start the project up in WorkerBee, treat that
as a request for a running app workload. Build needed local images, stage and
validate manifests, deploy with `workerbee_v1_manifest_deploy_local`, then
inspect status/logs and probe ingress. Do not stop after
`workerbee_v1_project_start` if deployable manifests or
Containerfiles/Dockerfiles exist.

If this is the first time WorkerBee is coming up for a project, there may be no
deployed workload to inspect yet. Prefer existing repo manifests and
Containerfiles/Dockerfiles. When they are absent, build a temporary native k1s
deployment in WorkerBee state, deploy it locally, then rerun the requested
runtime validation or security review. Keep first-run generated artifacts in
WorkerBee state unless the user asks to commit them.
{AGENT_INSTRUCTIONS_END}
"""


def install_agent_instructions(
    *,
    target: Path,
    check: bool = False,
    append: bool = False,
    allow_create: bool = False,
) -> dict[str, Any]:
    """Inspect or explicitly append the WorkerBee AGENTS.md instruction block."""
    path = target.expanduser().resolve()
    exists = path.exists()
    if exists and not path.is_file():
        raise WorkerBeeError(
            code="AGENTS_TARGET_NOT_FILE",
            message=f"AGENTS target is not a file: {path}",
            details={"path": str(path)},
            remediation="Pass a file path such as AGENTS.md.",
        )
    text = path.read_text(encoding="utf-8") if exists else ""
    installed = AGENT_INSTRUCTIONS_START in text and AGENT_INSTRUCTIONS_END in text
    if check:
        return {
            "ok": True,
            "path": str(path),
            "exists": exists,
            "installed": installed,
            "changed": False,
            "would_create": not exists,
            "would_append": exists and not installed,
        }
    if installed:
        return {
            "ok": True,
            "path": str(path),
            "exists": exists,
            "installed": True,
            "changed": False,
        }
    if not append:
        raise WorkerBeeError(
            code="AGENTS_APPEND_REQUIRED",
            message="AGENTS.md installation requires explicit --append",
            details={"path": str(path), "exists": exists},
            remediation="Run `workerbee agent install --append --target AGENTS.md`.",
        )
    if not exists and not allow_create:
        raise WorkerBeeError(
            code="AGENTS_NOT_FOUND",
            message=f"AGENTS target does not exist: {path}",
            details={"path": str(path)},
            remediation=(
                "Create AGENTS.md first or pass "
                "`workerbee agent install --append --allow-create`."
            ),
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    instructions = agent_instructions_markdown().rstrip()
    prefix = text.rstrip()
    content = f"{prefix}\n\n{instructions}\n" if prefix else f"{instructions}\n"
    path.write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "exists": True,
        "installed": True,
        "changed": True,
    }


def runbook_payload() -> dict[str, Any]:
    return {
        "title": "WorkerBee Cloud-Native Loop",
        "summary": (
            "Codex should keep editing and running repo-local commands normally, while using "
            "WorkerBee MCP for the local k1s runtime, image builds, manifest deploys, logs, "
            "HTTPS ingress probes, dashboards, lifecycle mode, cleanup, and exports."
        ),
        "default_mode": DEFAULT_PROJECT_MODE,
        "mode_semantics": {
            "lazy": "Reserve the project and wait until deploy/start before launching k1s.",
            "start": "Start the project stack during session bootstrap and future Codex sessions.",
            "stop": "Stop the project stack and block implicit WorkerBee starts/deploys.",
        },
        "loop": [
            "Call workerbee_v1_session_start(cwd, goal) and keep the returned project.",
            "Build images with local shell scripts or workerbee_v1_image_build.",
            "Use image_build dockerfile=... for repo-root builds with nested Dockerfiles.",
            "Prepare/stage manifests, validate them, deploy locally, then inspect status/logs.",
            "Use named stages or returned stage_dir values for manifest operations.",
            "Use app names and the optional namespace field for logs/exec, not container names.",
            "Probe WorkerBee HTTPS ingress through workerbee_v1_ingress_probe.",
            "Pass probe headers for signed request checks such as presigned S3 PUTs.",
            (
                "Treat bring/run/start the project up in WorkerBee as a request to "
                "deploy a running app workload with workerbee_v1_manifest_deploy_local; "
                "do not stop after workerbee_v1_project_start when deployable app inputs exist."
            ),
            (
                "For first-time projects, stage and deploy a native k1s workload before "
                "runtime review."
            ),
            "Use workerbee_v1_security_review_project when the user asks for security review.",
            "Iterate until the running app is correct, then export k1s/k8s/helm artifacts.",
        ],
        "first_run": [
            "A new project may have no deployed WorkerBee workload yet.",
            "Prefer existing manifests and Containerfiles/Dockerfiles from the repo.",
            (
                "When no deployable manifests exist, generate temporary native k1s staged "
                "artifacts in WorkerBee state and deploy them locally."
            ),
            (
                "Keep generated first-run artifacts out of the repo unless the user asks to "
                "commit them."
            ),
        ],
        "security_review": [
            "Call workerbee_v1_project_status before review.",
            "Call workerbee_v1_security_review_project for deployed project review.",
            "If review reports no deployment metadata, stage/deploy the app and rerun review.",
            "Summarize critical/high findings first, then include the report path.",
        ],
        "k1s_profile_loop": [
            "Use only with explicit direct-containerd WorkerBee MCP sessions.",
            (
                "For tandem k1s development, launch from k1s-workerbee and use the sibling "
                "../k1s checkout as k1s_root."
            ),
            "Prefer one agent for both repos; use separate agents only for separable work.",
            "Use a stable explicit project such as k1s-dev for nested profile work.",
            "Start a containerized profile with workerbee_v1_profile_start.",
            "Deploy staged manifests with workerbee_v1_manifest_deploy_local(target='profile').",
            (
                "Inspect with workerbee_v1_profile_workload_status and "
                "workerbee_v1_logs(target='profile')."
            ),
            (
                "Run workerbee_v1_profile_workload_validate for the bundled realtime "
                "WebSocket smoke test."
            ),
            "Restart profiles after k1s source changes; restart MCP after WorkerBee changes.",
            "See docs/k1s-dev-workflow.md for the complete tandem workflow.",
        ],
        "temporary_files": (
            "Use /tmp or WorkerBee state for one-off helper scripts unless the user asked for "
            "durable repo scripts."
        ),
    }


def next_actions_for_mode(mode: str, *, running: bool) -> list[str]:
    mode = normalize_project_mode(mode)
    if mode == "stop":
        return [
            "Report that WorkerBee is disabled for this project.",
            "Use `workerbee project mode start --project <project>` to re-enable it.",
        ]
    if running:
        return [
            "Use workerbee_v1_project_status to inspect the stack.",
            "Deploy or probe the app through WorkerBee when runtime validation is needed.",
        ]
    if mode == "start":
        return [
            "The stack should be starting now; inspect project status and dashboards.",
        ]
    return [
        "Keep WorkerBee lazy until deployment/runtime validation is needed.",
        "Call workerbee_v1_manifest_deploy_local or workerbee_v1_project_start to start the stack.",
    ]


def user_message_for_session(
    *,
    project: str,
    mode: str,
    running: bool,
    dashboard_url: str | None,
) -> str:
    if mode == "stop":
        return (
            f"WorkerBee is disabled for project `{project}`. It will not start or deploy until "
            f"the user runs `workerbee project mode start --project {project}` or asks the agent "
            "to switch modes."
        )
    if running and dashboard_url:
        return f"WorkerBee project `{project}` is running. Dashboard: {dashboard_url}"
    if running:
        return f"WorkerBee project `{project}` is running."
    if mode == "start":
        return f"WorkerBee project `{project}` is configured to start immediately."
    return (
        f"WorkerBee project `{project}` is in lazy mode. The stack will start only when deploy "
        "or explicit start is requested."
    )


def open_browser(url: str | None) -> bool:
    if not url:
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False
