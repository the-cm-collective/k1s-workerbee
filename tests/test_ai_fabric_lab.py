import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from workerbee.manifests import _load_yaml_documents, validate_stage

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = REPO_ROOT / "examples" / "ai-fabric-lab"
SCRIPT = REPO_ROOT / "scripts" / "dev" / "ai_fabric_lab.py"


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deployment(path: Path) -> dict[str, object]:
    docs = _load_yaml_documents(path.read_text(encoding="utf-8"))
    assert len(docs) == 1
    return docs[0]


def _service_port(path: Path) -> int:
    deployment = _deployment(path)
    spec = deployment["spec"]
    assert isinstance(spec, dict)
    service = spec["service"]
    assert isinstance(service, dict)
    return int(service["port"])


def _env_value(path: Path, name: str) -> str:
    deployment = _deployment(path)
    spec = deployment["spec"]
    assert isinstance(spec, dict)
    env = spec["env"]
    assert isinstance(env, list)
    for item in env:
        assert isinstance(item, dict)
        if item.get("name") == name:
            return str(item.get("value"))
    raise AssertionError(f"missing env {name} in {path}")


def test_ai_fabric_lab_static_bundle_validates() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(result.stdout)

    assert payload["ok"] is True
    assert payload["default_track"] == "baseline"
    assert payload["tracks"] == [
        "baseline",
        "legacy-smollm-smoke",
        "lora-adapter-smoke",
        "lora-plumbing",
        "quality",
        "smoke",
    ]
    assert payload["stage"]["ok"] is True


def test_ai_fabric_lab_has_quality_track_with_qwen_coordinator() -> None:
    model_tracks = json.loads((EXAMPLE_ROOT / "model-tracks.json").read_text(encoding="utf-8"))
    smoke = model_tracks["tracks"]["smoke"]
    baseline = model_tracks["tracks"]["baseline"]
    quality = model_tracks["tracks"]["quality"]
    lora_plumbing = model_tracks["tracks"]["lora-plumbing"]
    lora_adapter = model_tracks["tracks"]["lora-adapter-smoke"]
    legacy = model_tracks["tracks"]["legacy-smollm-smoke"]

    assert model_tracks["run_defaults"]["attention_backend"] == "TRITON_ATTN"
    assert smoke["coordinator"]["model"] == "Qwen/Qwen2.5-3B-Instruct-AWQ"
    assert smoke["expert"]["model"] == "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"
    assert baseline["coordinator"]["model"] == "Qwen/Qwen2.5-7B-Instruct-AWQ"
    assert baseline["coordinator"]["gpu_memory_utilization"] == 0.34
    assert baseline["expert"]["model"] == "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
    assert quality["coordinator"]["model"] == "Qwen/Qwen2.5-7B-Instruct-AWQ"
    assert quality["coordinator"]["gpu_memory_utilization"] == 0.34
    assert quality["expert"]["model"] == "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
    assert lora_plumbing["coordinator"]["model"] == "Qwen/Qwen2.5-3B-Instruct-AWQ"
    assert lora_plumbing["coordinator"]["enable_lora"] is False
    assert lora_plumbing["coordinator"]["max_model_len"] == 4096
    assert lora_plumbing["coordinator"]["gpu_memory_utilization"] == 0.38
    assert lora_plumbing["expert"]["model"] == "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"
    assert lora_plumbing["expert"]["enable_lora"] is True
    assert lora_plumbing["expert"]["max_model_len"] == 4096
    assert lora_plumbing["expert"]["gpu_memory_utilization"] == 0.44
    assert lora_adapter["coordinator"]["model"] == "Qwen/Qwen2.5-3B-Instruct-AWQ"
    assert lora_adapter["expert"]["model"] == "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"
    assert lora_adapter["expert"]["enable_lora"] is True
    assert lora_adapter["expert"]["max_loras"] == 1
    assert lora_adapter["expert"]["max_lora_rank"] == 16
    assert lora_adapter["expert"]["lora_modules"] == [
        {
            "name": "k1s-code-expert-lora-smoke",
            "path": "/adapters/expert/validation",
            "base_model_name": "Qwen/Qwen2.5-Coder-7B-Instruct",
            "max_lora_rank": 16,
        }
    ]
    assert legacy["coordinator"]["model"] == "HuggingFaceTB/SmolLM3-3B"
    for track in model_tracks["tracks"].values():
        for lane in ("coordinator", "expert"):
            assert len(track[lane]["revision"]) == 40


def test_ai_fabric_lab_expert_served_name_uses_code_expert_convention() -> None:
    model_tracks = json.loads((EXAMPLE_ROOT / "model-tracks.json").read_text(encoding="utf-8"))
    router = _load_module(EXAMPLE_ROOT / "images" / "router" / "app.py", "ai_fabric_router")

    assert router.EXPERT_MODEL == "k1s-code-expert"
    for track in model_tracks["tracks"].values():
        assert track["expert"]["served_model_name"] == "k1s-code-expert"


def test_ai_fabric_lab_stage_is_workerbee_valid() -> None:
    validation = validate_stage(EXAMPLE_ROOT / "stage")

    assert validation["ok"] is True
    assert validation["input_kinds"] == ["native-k1s"]
    assert "localhost/workerbee-ai-fabric-models:dev" in validation["images"]
    assert "ai-fabric-lab/ai-coordinator" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-expert" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-router" in validation["required_controller_scopes"]


def test_ai_fabric_lab_plumbing_stage_is_workerbee_valid() -> None:
    validation = validate_stage(EXAMPLE_ROOT / "stage-plumbing")

    assert validation["ok"] is True
    assert validation["input_kinds"] == ["native-k1s"]
    assert "localhost/workerbee-ai-fabric-fake-model:dev" in validation["images"]
    assert "localhost/workerbee-ai-fabric-models:dev" not in validation["images"]
    assert "ai-fabric-lab/fake-model" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-router" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/das-bridge" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-coordinator" not in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-expert" not in validation["required_controller_scopes"]


def test_ai_fabric_lab_baseline_stage_is_workerbee_valid() -> None:
    validation = validate_stage(EXAMPLE_ROOT / "stage-baseline")

    assert validation["ok"] is True
    assert validation["input_kinds"] == ["native-k1s"]
    assert "localhost/workerbee-ai-fabric-models:dev" in validation["images"]
    assert "ai-fabric-lab/ai-coordinator" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-expert" in validation["required_controller_scopes"]
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-baseline" / "manifests" / "ai-coordinator.yaml",
            "AI_FABRIC_TRACK",
        )
        == "baseline"
    )
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-baseline" / "manifests" / "ai-expert.yaml",
            "AI_FABRIC_TRACK",
        )
        == "baseline"
    )
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-baseline" / "manifests" / "ai-router.yaml",
            "AI_ROUTER_ADVISORY_MODEL_TIMEOUT",
        )
        == "180"
    )


def test_ai_fabric_lab_quality_stage_is_workerbee_valid() -> None:
    validation = validate_stage(EXAMPLE_ROOT / "stage-quality")

    assert validation["ok"] is True
    assert validation["input_kinds"] == ["native-k1s"]
    assert "localhost/workerbee-ai-fabric-models:dev" in validation["images"]
    assert "ai-fabric-lab/ai-coordinator" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-expert" in validation["required_controller_scopes"]
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-quality" / "manifests" / "ai-coordinator.yaml",
            "AI_FABRIC_TRACK",
        )
        == "quality"
    )
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-quality" / "manifests" / "ai-expert.yaml",
            "AI_FABRIC_TRACK",
        )
        == "quality"
    )


def test_ai_fabric_lab_lora_plumbing_stage_is_workerbee_valid() -> None:
    validation = validate_stage(EXAMPLE_ROOT / "stage-lora-plumbing")

    assert validation["ok"] is True
    assert validation["input_kinds"] == ["native-k1s"]
    assert "localhost/workerbee-ai-fabric-models:dev" in validation["images"]
    assert "ai-fabric-lab/ai-coordinator" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-expert" in validation["required_controller_scopes"]
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-lora-plumbing" / "manifests" / "ai-coordinator.yaml",
            "AI_FABRIC_TRACK",
        )
        == "lora-plumbing"
    )
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-lora-plumbing" / "manifests" / "ai-expert.yaml",
            "AI_FABRIC_TRACK",
        )
        == "lora-plumbing"
    )


def test_ai_fabric_lab_lora_adapter_smoke_stage_is_workerbee_valid() -> None:
    validation = validate_stage(EXAMPLE_ROOT / "stage-lora-adapter-smoke")

    assert validation["ok"] is True
    assert validation["input_kinds"] == ["native-k1s"]
    assert "localhost/workerbee-ai-fabric-models:dev" in validation["images"]
    assert "ai-fabric-lab/ai-coordinator" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-expert" in validation["required_controller_scopes"]
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-lora-adapter-smoke" / "manifests" / "ai-coordinator.yaml",
            "AI_FABRIC_TRACK",
        )
        == "lora-adapter-smoke"
    )
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-lora-adapter-smoke" / "manifests" / "ai-expert.yaml",
            "AI_FABRIC_TRACK",
        )
        == "lora-adapter-smoke"
    )
    assert (
        _env_value(
            EXAMPLE_ROOT / "stage-lora-adapter-smoke" / "manifests" / "ai-router.yaml",
            "AI_ROUTER_ADVISORY_MODEL_TIMEOUT",
        )
        == "75"
    )


def test_ai_fabric_lab_stages_use_dedicated_workerbee_service_ports() -> None:
    for stage in (
        "stage",
        "stage-baseline",
        "stage-quality",
        "stage-lora-plumbing",
        "stage-lora-adapter-smoke",
        "stage-plumbing",
    ):
        manifests = EXAMPLE_ROOT / stage / "manifests"

        assert _service_port(manifests / "ai-router.yaml") == 18180
        assert _service_port(manifests / "das-bridge.yaml") == 18181
        assert _service_port(manifests / "retrieval-indexer.yaml") == 18182


def test_ai_fabric_lab_runtime_prompt_fixture_is_valid() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_prompt_test")
    prompts = lab._load_prompt_suite(EXAMPLE_ROOT / "prompts" / "validation-suite.jsonl")

    assert {item["suite"] for item in prompts} == {"mixed-soak", "quality-contract"}
    assert {"coordinator", "expert"} == {item["lane"] for item in prompts}
    assert len({item["id"] for item in prompts}) == len(prompts)


def test_ai_fabric_runtime_url_candidates_include_cluster_fallback() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_url_fallback_candidates")

    candidates = lab._runtime_url_candidates(
        url="http://127.0.0.1:18180",
        role="router",
    )

    assert candidates == [
        "http://127.0.0.1:18180",
        "http://ai-router.ai-fabric-lab.svc.cluster.local:8080",
    ]


def test_ai_fabric_lab_resolves_runtime_endpoint_to_service_alias_on_local_refusal(
    monkeypatch,
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_endpoint_resolve_test")
    calls: list[tuple[str, int]] = []

    def fake_get_json(url: str, timeout: int) -> dict[str, object]:
        calls.append((url, timeout))
        if "127.0.0.1:18180/healthz" in url:
            return {"ok": False, "error": "connection-refused"}
        if "ai-router.ai-fabric-lab.svc.cluster.local:8080/healthz" in url:
            return {"ok": True, "service": "ai-router"}
        return {"ok": False, "error": "unexpected"}

    monkeypatch.setattr(lab, "_get_json", fake_get_json)

    resolved = lab._resolve_runtime_endpoint("http://127.0.0.1:18180", "router", 1)

    assert resolved == "http://ai-router.ai-fabric-lab.svc.cluster.local:8080"
    assert any("127.0.0.1:18180/healthz" in url for url, _ in calls)
    assert any(
        "ai-router.ai-fabric-lab.svc.cluster.local:8080/healthz" in url for url, _ in calls
    )


def test_ai_fabric_lab_validate_runtime_uses_resolved_runtime_endpoints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_validation_resolution")
    captured: dict[str, str] = {}

    def fake_resolve(
        *,
        router_url: str,
        das_url: str,
        retrieval_url: str,
        timeout_seconds: int,
    ) -> dict[str, str]:
        captured["inputs"] = f"{router_url}|{das_url}|{retrieval_url}|{timeout_seconds}"
        return {
            "router_url": "http://ai-router.ai-fabric-lab.svc.cluster.local:8080",
            "das_url": "http://das-bridge.ai-fabric-lab.svc.cluster.local:8081",
            "retrieval_url": "http://retrieval-indexer.ai-fabric-lab.svc.cluster.local:8082",
        }

    monkeypatch.setattr(
        lab,
        "_resolve_runtime_endpoints",
        fake_resolve,
    )
    monkeypatch.setattr(
        lab,
        "_health_snapshot",
        lambda **_: {
            "ok": True,
            "checked_at": "2026-06-02T00:00:00+00:00",
            "endpoints": {},
        },
    )
    monkeypatch.setattr(
        lab,
        "_host_alias_snapshot",
        lambda **_: {
            "ok": True,
            "checked_at": "2026-06-02T00:00:00+00:00",
            "endpoints": {},
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_evidence_closeout",
        lambda **_: {"ok": True, "record_count": 0, "findings": []},
    )

    result = lab.validate_runtime(
        EXAMPLE_ROOT,
        suite="evidence-closeout",
        prompts=None,
        storage_root=tmp_path,
        run_id="evidence-closeout-test",
        track=None,
        router_url="http://127.0.0.1:18180",
        das_url="http://127.0.0.1:18181",
        retrieval_url="http://127.0.0.1:18182",
        duration_seconds=None,
        workers=None,
        worker_sleep_seconds=0,
        gpu_sample_seconds=None,
        request_timeout=1,
        success_threshold=0.95,
        vram_growth_mib_max=4096,
    )

    assert result["router_url"] == "http://ai-router.ai-fabric-lab.svc.cluster.local:8080"
    assert result["das_url"] == "http://das-bridge.ai-fabric-lab.svc.cluster.local:8081"
    assert result["retrieval_url"] == "http://retrieval-indexer.ai-fabric-lab.svc.cluster.local:8082"
    assert captured["inputs"] == (
        "http://127.0.0.1:18180|"
        "http://127.0.0.1:18181|"
        "http://127.0.0.1:18182|1"
    )


def test_ai_fabric_lab_advisor_scenario_fixture_is_valid() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_advisor_scenario_fixture_test")
    scenarios = lab._load_advisor_scenarios(
        EXAMPLE_ROOT / "prompts" / "advisor-scenarios.jsonl"
    )

    assert {
        "healthy-ai-router",
        "degraded-das-dependency",
        "unavailable-retrieval-dependency",
        "unavailable-expert-model-lane",
        "missing-symbolic-evidence",
        "stale-validation-artifact",
        "fabric-f3-blocked-by-f1-f2",
        "fabric-f5-blocked-by-f3",
        "missing-phase-evidence",
        "stale-phase-report",
        "lora-adapter-ready",
        "lora-adapter-invalid",
    } == {item["id"] for item in scenarios}
    assert all(item["expect"]["authoritative"] is False for item in scenarios)


def test_ai_fabric_lab_runtime_suite_contract() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_suite_test")

    assert "acceptance-closeout" in lab.RUNTIME_SUITE_CHOICES
    assert "adapter-preflight" in lab.RUNTIME_SUITE_CHOICES
    assert "lora-adapter-smoke" in lab.RUNTIME_SUITE_CHOICES
    assert "quality-comparison" in lab.RUNTIME_SUITE_CHOICES
    assert "stress-burst" in lab.RUNTIME_SUITE_CHOICES
    assert "recovery-smoke" in lab.RUNTIME_SUITE_CHOICES
    assert "advisor-scenarios" in lab.RUNTIME_SUITE_CHOICES
    assert "advisor-scenarios" in lab.RUNTIME_ENDPOINT_SUITES
    assert "advisor-scenarios" not in lab.RUNTIME_MODEL_SUITES
    assert "advisor-scenarios" not in lab.RUNTIME_PROMPT_SUITES
    assert lab.ADVISOR_SCENARIO_EVAL_API_VERSION == (
        "workerbee.ai-fabric.advisor-scenario-eval/v1"
    )
    assert lab._selected_runtime_suites("all") == [
        "quality-contract",
        "mixed-soak",
        "evidence-closeout",
    ]
    assert lab._selected_runtime_suites("acceptance-closeout") == [
        "adapter-preflight",
        "lora-adapter-smoke",
        "quality-comparison",
        "stress-burst",
        "recovery-smoke",
        "advisor-scenarios",
        "evidence-closeout",
    ]
    assert lab._runtime_defaults_for_suite(
        suite="stress-burst",
        duration_seconds=None,
        workers=None,
        gpu_sample_seconds=None,
    ) == {"duration_seconds": 900, "workers": 6, "gpu_sample_seconds": 15}
    assert lab._runtime_defaults_for_suite(
        suite="acceptance-closeout",
        duration_seconds=None,
        workers=None,
        gpu_sample_seconds=None,
    ) == {"duration_seconds": 900, "workers": 6, "gpu_sample_seconds": 15}


def test_ai_fabric_runtime_relationship_vocabulary_is_stable() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_relationship_vocabulary_test")

    assert lab.ADVISORY_DECISION_API_VERSION == "workerbee.ai-fabric.advisory-decision/v1"
    assert lab.RUNTIME_RELATIONSHIP_PREDICATES == (
        "owns_service",
        "depends_on",
        "serves_model",
        "requires_resource",
        "produced_artifact",
        "supports_advisory",
    )


def test_ai_fabric_runtime_facts_cover_services_dependencies_and_lora(
    tmp_path: Path,
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_facts_test")
    tracks = json.loads((EXAMPLE_ROOT / "model-tracks.json").read_text(encoding="utf-8"))[
        "tracks"
    ]

    facts = lab._runtime_facts(
        project="k1s-workerbee-test",
        track="lora-adapter-smoke",
        config=tracks["lora-adapter-smoke"],
        k1s_root=tmp_path / "missing-k1s",
        stage_dir=EXAMPLE_ROOT / "stage-lora-adapter-smoke",
    )

    assert {
        "namespace": "runtime",
        "subject": "workerbee.project",
        "predicate": "owns_service",
        "object": "ai_fabric.service.ai-router",
        "source": "workerbee.ai-fabric.runtime-facts/v1",
    } in facts
    assert {
        "namespace": "runtime",
        "subject": "ai_fabric.service.ai-router",
        "predicate": "depends_on",
        "object": "ai_fabric.service.das-bridge",
        "source": "workerbee.ai-fabric.runtime-facts/v1",
    } in facts
    assert {
        "namespace": "runtime",
        "subject": "ai_fabric.service.ai-expert",
        "predicate": "serves_model",
        "object": "ai_fabric.adapter.k1s-code-expert-lora-smoke",
        "source": "workerbee.ai-fabric.runtime-facts/v1",
    } in facts
    assert {
        "namespace": "runtime",
        "subject": "ai_fabric.service.das-bridge",
        "predicate": "supports_advisory",
        "object": "symbolic_evidence",
        "source": "workerbee.ai-fabric.runtime-facts/v1",
    } in facts
    assert any(
        fact["subject"] == "ai_fabric.service.ai-router"
        and fact["predicate"] == "host_alias"
        and fact["object"] == "http://127.0.0.1:18180"
        for fact in facts
    )
    assert any(
        fact["subject"] == "ai_fabric.runtime_validation"
        and fact["predicate"] == "produced_artifact"
        and str(fact["object"]).endswith("/summary.json")
        for fact in facts
    )
    assert any(
        fact["subject"] == "ai_fabric.advisory_decision"
        and fact["predicate"] == "api_version"
        and fact["object"] == "workerbee.ai-fabric.advisory-decision/v1"
        for fact in facts
    )


def test_ai_fabric_runtime_state_facts_capture_live_endpoint_snapshots(monkeypatch) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_state_facts_test")

    def fake_get_json(url: str, *, timeout: int) -> dict[str, object]:
        del timeout
        if url.endswith("/healthz") and "18180" in url:
            return {"ok": True, "service": "ai-router"}
        if url.endswith("/healthz") and "18182" in url:
            return {
                "ok": True,
                "service": "retrieval-indexer",
                "document_count": 7,
                "chunk_count": 11,
                "qdrant_indexed": True,
            }
        if "/v1/models?lane=coordinator" in url:
            return {"ok": True, "data": [{"id": "general-coordinator"}]}
        if "/v1/models?lane=expert" in url:
            return {"ok": False, "data": [], "errors": {"expert": "warming"}}
        return {"ok": False, "error": "unexpected"}

    monkeypatch.setattr(lab, "_get_json", fake_get_json)

    facts = lab._runtime_state_facts(
        router_url="http://127.0.0.1:18180",
        das_url="http://das.local:18181",
        retrieval_url="http://127.0.0.1:18182",
        workerbee_status=None,
    )

    assert {
        "namespace": "runtime",
        "subject": "ai_fabric.service.ai-router",
        "predicate": "readiness",
        "object": "ready",
        "source": "workerbee.ai-fabric.runtime-facts/v1",
    } in facts
    assert {
        "namespace": "runtime",
        "subject": "ai_fabric.service.das-bridge",
        "predicate": "readiness",
        "object": "unknown",
        "source": "workerbee.ai-fabric.runtime-facts/v1",
    } in facts
    assert any(
        fact["subject"] == "ai_fabric.retrieval_corpus"
        and fact["predicate"] == "document_count"
        and fact["object"] == 7
        for fact in facts
    )
    assert any(
        fact["subject"] == "ai_fabric.model.expert"
        and fact["predicate"] == "model_lane_readiness"
        and fact["object"]["ok"] is False
        for fact in facts
    )


def test_ai_fabric_lab_advisor_scenarios_validate_das_decisions(
    tmp_path: Path, monkeypatch
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_advisor_scenario_suite_test")
    das_bridge = _load_module(
        EXAMPLE_ROOT / "images" / "das-bridge" / "app.py",
        "ai_fabric_das_bridge_advisor_scenario_suite_test",
    )
    monkeypatch.setattr(das_bridge, "DATA_DIR", tmp_path / "das")
    monkeypatch.setattr(das_bridge, "FACT_LOG", tmp_path / "facts.jsonl")
    monkeypatch.setattr(das_bridge, "F5_EVIDENCE_LOG", tmp_path / "f5-evidence.jsonl")

    def fake_post_json(
        url: str, payload: dict[str, object], *, timeout: int
    ) -> dict[str, object]:
        del timeout
        assert url.endswith("/v1/advisory/decision")
        assert payload["use_stored_facts"] is False
        decision = das_bridge._advisory_decision(payload)
        return {"ok": True, "status": 200, "json": {"ok": True, "decision": decision}}

    def fake_runtime_state_facts(**kwargs) -> list[dict[str, object]]:
        del kwargs
        return [
            lab._runtime_fact(
                "ai_fabric.service.ai-router",
                "depends_on",
                "ai_fabric.service.das-bridge",
            ),
            lab._runtime_fact(
                "ai_fabric.service.ai-router",
                "depends_on",
                "ai_fabric.service.retrieval-indexer",
            ),
            lab._runtime_fact("ai_fabric.service.das-bridge", "readiness", "ready"),
            lab._runtime_fact("ai_fabric.service.retrieval-indexer", "readiness", "ready"),
        ]

    monkeypatch.setattr(lab, "_post_json", fake_post_json)
    monkeypatch.setattr(lab, "_runtime_state_facts", fake_runtime_state_facts)
    scenarios = lab._load_advisor_scenarios(
        EXAMPLE_ROOT / "prompts" / "advisor-scenarios.jsonl"
    )
    output_path = tmp_path / "advisor-scenarios.json"

    result = lab._run_advisor_scenarios(
        scenarios=scenarios,
        das_url="http://das.local:18181",
        router_url="http://router.local:18180",
        retrieval_url="http://retrieval.local:18182",
        run_id="advisor-scenario-test",
        output_path=output_path,
        request_timeout=1,
    )

    assert result["ok"] is True
    assert result["api_version"] == "workerbee.ai-fabric.advisor-scenario-eval/v1"
    assert result["scenario_count"] == len(scenarios) + 1
    assert {item["kind"] for item in result["results"]} == {"synthetic", "live"}
    assert output_path.exists()


def test_ai_fabric_import_runtime_facts_batches_to_das(monkeypatch) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_import_batch_test")
    posted: list[dict[str, object]] = []

    def fake_post(
        das_url: str,
        facts: list[dict[str, object]],
        findings: list[dict[str, str]],
    ) -> dict[str, object]:
        del findings
        posted.extend(facts)
        return {"ok": True, "url": das_url, "imported": len(facts)}

    monkeypatch.setattr(lab, "_post_runtime_facts", fake_post)

    result = lab.import_runtime_facts(
        EXAMPLE_ROOT,
        stage=EXAMPLE_ROOT / "stage-lora-adapter-smoke",
        das_url="http://das.local",
        project="k1s-workerbee-test",
        track="lora-adapter-smoke",
        k1s_root=REPO_ROOT.parent / "missing-k1s",
    )

    assert result["ok"] is True
    assert result["posted"]["imported"] == len(posted)
    assert posted
    assert all(item["namespace"] == "runtime" for item in posted)


def test_ai_fabric_lab_adapter_preflight_blocks_without_payload(tmp_path: Path) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_adapter_preflight_test")

    result = lab._run_adapter_preflight(storage_root=tmp_path)

    assert result["ok"] is True
    assert result["state"] == "blocked"
    assert result["blocked"] is True
    assert "adapter_config_present" in result["missing"]


def test_ai_fabric_lab_adapter_preflight_validates_ready_payload(tmp_path: Path) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_adapter_preflight_ready_test")
    adapter = tmp_path / "adapters" / "expert" / "validation"
    adapter.mkdir(parents=True)
    (adapter / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": "Qwen/Qwen2.5-Coder-7B-Instruct",
                "r": 16,
                "target_modules": ["q_proj", "k_proj"],
            }
        ),
        encoding="utf-8",
    )
    (adapter / "adapter_model.safetensors").write_text("", encoding="utf-8")

    result = lab._run_adapter_preflight(storage_root=tmp_path)

    assert result["ok"] is True
    assert result["state"] == "ready"
    assert result["blocked"] is False
    assert result["metadata"]["base_model_name"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert result["metadata"]["rank"] == 16
    assert result["checks"]["adapter_target_modules_present"] is True


def test_ai_fabric_lab_adapter_preflight_rejects_invalid_payload(tmp_path: Path) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_adapter_preflight_invalid_test")
    adapter = tmp_path / "adapters" / "expert" / "validation"
    adapter.mkdir(parents=True)
    (adapter / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": "Other/Model",
                "r": 64,
                "target_modules": [],
            }
        ),
        encoding="utf-8",
    )
    (adapter / "adapter_model.safetensors").write_text("", encoding="utf-8")

    result = lab._run_adapter_preflight(storage_root=tmp_path)

    assert result["ok"] is False
    assert result["state"] == "invalid"
    assert "adapter_base_model_expected" in result["invalid"]
    assert "adapter_rank_valid" in result["invalid"]
    assert "adapter_target_modules_present" in result["invalid"]


def test_ai_fabric_lab_lora_adapter_smoke_blocks_without_payload(tmp_path: Path) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_lora_adapter_blocked_test")
    requests_path = tmp_path / "requests.jsonl"

    result = lab._run_lora_adapter_smoke(
        storage_root=tmp_path,
        router_url="http://127.0.0.1:1",
        run_id="lora-adapter-blocked-test",
        requests_path=requests_path,
        request_timeout=1,
    )

    assert result["ok"] is True
    assert result["state"] == "blocked"
    assert result["blocked"] is True
    assert not requests_path.exists()


def test_ai_fabric_lab_adapter_preflight_suite_skips_runtime_health(tmp_path: Path) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_adapter_preflight_suite_test")

    result = lab.validate_runtime(
        EXAMPLE_ROOT,
        suite="adapter-preflight",
        prompts=None,
        storage_root=tmp_path,
        run_id="adapter-preflight-test",
        track=None,
        router_url="http://127.0.0.1:1",
        das_url="http://127.0.0.1:2",
        retrieval_url="http://127.0.0.1:3",
        duration_seconds=None,
        workers=None,
        worker_sleep_seconds=0,
        gpu_sample_seconds=None,
        request_timeout=1,
        success_threshold=0.95,
        vram_growth_mib_max=4096,
    )

    assert result["ok"] is True
    assert result["health"]["skipped"] is True
    assert result["host_aliases"]["skipped"] is True
    assert result["blocked_items"][0]["suite"] == "adapter-preflight"


def test_ai_fabric_lab_lora_adapter_smoke_skips_runtime_when_payload_missing(
    tmp_path: Path,
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_lora_adapter_suite_blocked_test")

    result = lab.validate_runtime(
        EXAMPLE_ROOT,
        suite="lora-adapter-smoke",
        prompts=None,
        storage_root=tmp_path,
        run_id="lora-adapter-smoke-blocked-test",
        track=None,
        router_url="http://127.0.0.1:1",
        das_url="http://127.0.0.1:2",
        retrieval_url="http://127.0.0.1:3",
        duration_seconds=None,
        workers=None,
        worker_sleep_seconds=0,
        gpu_sample_seconds=None,
        request_timeout=1,
        success_threshold=0.95,
        vram_growth_mib_max=4096,
    )

    assert result["ok"] is True
    assert result["health"]["skipped"] is True
    assert result["lane_readiness"]["skipped"] is True
    assert result["blocked_items"][0]["suite"] == "lora-adapter-smoke"


def test_ai_fabric_lab_model_launcher_emits_static_lora_flags() -> None:
    launcher = _load_module(
        EXAMPLE_ROOT / "images" / "ai-models" / "run_two_vllm.py",
        "ai_fabric_model_launcher_lora_test",
    )

    command = launcher._vllm_command(
        {
            "port": 8002,
            "model": "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ",
            "revision": "8e8ed243bbe6f9a5aff549a0924562fc719b2b8a",
            "served_model_name": "k1s-code-expert",
            "max_model_len": 4096,
            "gpu_memory_utilization": 0.44,
            "quantization": "awq",
            "enable_lora": True,
            "max_loras": 1,
            "max_lora_rank": 16,
            "lora_modules": [
                {"name": "k1s-code-expert-lora-smoke", "path": "/adapters/expert/validation"}
            ],
        },
        defaults={"attention_backend": "TRITON_ATTN"},
        download_dir="/models/hf-cache",
    )

    assert "--enable-lora" in command
    assert "--lora-modules" in command
    assert "k1s-code-expert-lora-smoke=/adapters/expert/validation" in command
    assert command[command.index("--max-lora-rank") + 1] == "16"
    assert command[command.index("--max-loras") + 1] == "1"


def test_ai_fabric_lab_runtime_output_files_contract() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_output_test")

    assert set(lab.RUNTIME_OUTPUT_FILES) == {
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
    }


def test_ai_fabric_lab_runtime_profile_records_mixed_soak_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_profile_soak_test")

    monkeypatch.setattr(
        lab,
        "_resolve_runtime_endpoints",
        lambda **kwargs: {
            "router_url": kwargs["router_url"],
            "das_url": kwargs["das_url"],
            "retrieval_url": kwargs["retrieval_url"],
        },
    )
    monkeypatch.setattr(
        lab,
        "_health_snapshot",
        lambda **kwargs: {
            "ok": True,
            "checked_at": "2026-06-03T00:00:00+00:00",
            "endpoints": {
                "router": {"ok": True, "service": "ai-router"},
                "das": {"ok": True, "service": "das-bridge", "fact_count": 12},
                "retrieval": {
                    "ok": True,
                    "service": "retrieval-indexer",
                    "document_count": 3,
                    "chunk_count": 9,
                },
            },
        },
    )
    monkeypatch.setattr(
        lab,
        "_host_alias_snapshot",
        lambda **kwargs: {"ok": True, "checked_at": "2026-06-03T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        lab,
        "_lane_readiness_snapshot",
        lambda **kwargs: {
            "ok": True,
            "lanes": {
                "coordinator": {"ok": True, "model_id": "general-coordinator"},
                "expert": {"ok": True, "model_id": "k1s-code-expert"},
            },
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_quality_contract",
        lambda **kwargs: {"ok": True, "results": [], "findings": []},
    )
    monkeypatch.setattr(
        lab,
        "_run_mixed_soak",
        lambda **kwargs: {
            "ok": True,
            "duration_seconds": kwargs["duration_seconds"],
            "workers": kwargs["workers"],
            "request_count": 10,
            "ok_count": 10,
            "success_rate": 1.0,
            "gpu_sample_count": 2,
            "final_vram_growth_mib": 12,
            "vram_growth_mib_max": kwargs["vram_growth_mib_max"],
            "findings": [],
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_evidence_closeout",
        lambda **kwargs: {"ok": True, "record_count": 3, "findings": []},
    )

    result = lab.validate_runtime(
        EXAMPLE_ROOT,
        suite="all",
        prompts=None,
        storage_root=tmp_path,
        run_id="baseline-soak-evidence-test",
        track="baseline",
        router_url="http://127.0.0.1:18180",
        das_url="http://127.0.0.1:18181",
        retrieval_url="http://127.0.0.1:18182",
        duration_seconds=60,
        workers=2,
        worker_sleep_seconds=0,
        gpu_sample_seconds=5,
        request_timeout=1,
        success_threshold=0.95,
        vram_growth_mib_max=4096,
    )

    profile = json.loads(
        (Path(result["run_dir"]) / "ai-runtime-profile.json").read_text(encoding="utf-8")
    )
    operator_report = json.loads(
        (Path(result["run_dir"]) / "operator-report.json").read_text(encoding="utf-8")
    )
    soak = profile["evidence"]["soak"]

    assert result["ok"] is True
    assert profile["track"] == "baseline"
    assert profile["observed_vram_growth_mib"] == 12
    assert soak == {
        "suite": "mixed-soak",
        "track": "baseline",
        "ok": True,
        "duration_seconds": 60,
        "workers": 2,
        "request_count": 10,
        "success_rate": 1.0,
        "gpu_sample_count": 2,
        "final_vram_growth_mib": 12,
        "vram_growth_mib_max": 4096,
        "promotion_duration_seconds": 1800,
        "promotion_ready": False,
    }
    assert (
        "Baseline or quality soak evidence is present but 60s is below the 1800s promotion threshold."
        in operator_report["known_gaps"]
    )


def test_ai_fabric_lab_normalizes_workerbee_cli_project_status() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_workerbee_status_normalize_test")

    status = lab._normalize_workerbee_status_payload(
        {
            "project": "k1s-workerbee-dev-2592c13f5e",
            "mode": "start",
            "project_status": {
                "running": True,
                "stack": {"runtime": "containerd"},
                "app_status": {
                    "ready": True,
                    "ready_workload_count": 8,
                    "degraded_workload_count": 0,
                },
            },
        }
    )

    assert status["api_version"] == "workerbee.mcp/v1"
    assert status["kind"] == "ProjectStatus"
    assert status["ok"] is True
    assert status["source"] == "workerbee.cli.project_status"
    assert status["data"]["app_status"]["ready_workload_count"] == 8


def test_ai_fabric_lab_normalizes_workerbee_mcp_project_status() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_workerbee_mcp_status_normalize_test")

    status = lab._normalize_workerbee_status_payload(
        {
            "api_version": "workerbee.mcp/v1",
            "kind": "ProjectStatus",
            "ok": True,
            "project": "k1s-workerbee-dev-2592c13f5e",
            "data": {"running": True, "app_status": {"ready": True}},
        }
    )

    assert status["api_version"] == "workerbee.mcp/v1"
    assert status["kind"] == "ProjectStatus"
    assert status["ok"] is True
    assert status["source"] == "workerbee.mcp.project_status"


def test_ai_fabric_lab_acceptance_closeout_writes_contract_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_acceptance_closeout_test")
    storage_root = tmp_path / "lab"
    adapter = storage_root / "adapters" / "expert" / "validation"
    adapter.mkdir(parents=True)
    (adapter / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": "Qwen/Qwen2.5-Coder-7B-Instruct",
                "r": 16,
                "target_modules": ["q_proj", "k_proj"],
            }
        ),
        encoding="utf-8",
    )
    (adapter / "adapter_model.safetensors").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        lab,
        "_health_snapshot",
        lambda **kwargs: {
            "ok": True,
            "checked_at": "2026-06-02T00:00:00+00:00",
            "endpoints": {
                "router": {"ok": True, "service": "ai-router"},
                "das": {
                    "ok": True,
                    "service": "das-bridge",
                    "fact_count": 12,
                    "f5_evidence_count": 7,
                },
                "retrieval": {
                    "ok": True,
                    "service": "retrieval-indexer",
                    "document_count": 3,
                    "chunk_count": 9,
                },
            },
        },
    )
    monkeypatch.setattr(
        lab,
        "_host_alias_snapshot",
        lambda **kwargs: {"ok": True, "checked_at": "2026-06-02T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        lab,
        "_lane_readiness_snapshot",
        lambda **kwargs: {
            "ok": True,
            "lanes": {
                "coordinator": {"ok": True, "model_id": "general-coordinator"},
                "expert": {"ok": True, "model_id": "k1s-code-expert"},
            },
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_lora_adapter_smoke",
        lambda **kwargs: {
            "ok": True,
            "preflight": kwargs["preflight"],
            "adapter_model": "k1s-code-expert-lora-smoke",
            "base_model": "k1s-code-expert",
            "findings": [],
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_quality_comparison",
        lambda **kwargs: {
            "ok": True,
            "results": [
                {
                    "ok": True,
                    "trace_id": "trace-acceptance",
                    "trace_path": "/data/traces/trace-acceptance.json",
                }
            ],
            "findings": [],
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_stress_burst",
        lambda **kwargs: {
            "ok": True,
            "final_vram_growth_mib": 12,
            "request_count": 4,
            "ok_count": 4,
            "findings": [],
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_recovery_smoke",
        lambda **kwargs: {"ok": True, "results": [], "findings": []},
    )
    monkeypatch.setattr(
        lab,
        "_run_advisor_scenarios",
        lambda **kwargs: {"ok": True, "scenario_count": 2, "findings": []},
    )
    monkeypatch.setattr(
        lab,
        "_run_evidence_closeout",
        lambda **kwargs: {"ok": True, "record_count": 3, "findings": []},
    )

    result = lab.validate_runtime(
        EXAMPLE_ROOT,
        suite="acceptance-closeout",
        prompts=None,
        storage_root=storage_root,
        run_id="acceptance-closeout-test",
        track=None,
        router_url="http://127.0.0.1:18180",
        das_url="http://127.0.0.1:18181",
        retrieval_url="http://127.0.0.1:18182",
        duration_seconds=None,
        workers=None,
        worker_sleep_seconds=0,
        gpu_sample_seconds=None,
        request_timeout=1,
        success_threshold=0.95,
        vram_growth_mib_max=4096,
    )

    run_dir = Path(result["run_dir"])
    acceptance = json.loads((run_dir / "acceptance.json").read_text(encoding="utf-8"))
    profile = json.loads((run_dir / "ai-runtime-profile.json").read_text(encoding="utf-8"))
    operator_report = json.loads(
        (run_dir / "operator-report.json").read_text(encoding="utf-8")
    )

    assert result["ok"] is True
    assert result["track"] == "lora-adapter-smoke"
    assert acceptance["api_version"] == "workerbee.ai-fabric.acceptance-run/v1"
    assert acceptance["ok"] is True
    assert profile["api_version"] == "k1s.fabric.ai-runtime-profile/v1"
    assert profile["controller_authority"] == "k1s"
    assert profile["model_lanes"]["expert"]["served_model_name"] == "k1s-code-expert"
    assert profile["adapter_hotset"][0]["name"] == "k1s-code-expert-lora-smoke"
    assert profile["observed_vram_growth_mib"] == 12
    assert profile["evidence"]["das_fact_count"] == 12
    assert profile["evidence"]["retrieval_corpus_count"]["chunk_count"] == 9
    assert profile["evidence"]["advisory_trace_refs"][0]["trace_id"] == "trace-acceptance"
    assert operator_report["api_version"] == "workerbee.ai-fabric.operator-report/v1"
    assert operator_report["recommended_next_action"].startswith("promote")
    assert (
        "Final WorkerBee MCP project status should be refreshed in workerbee-status.json before promotion."
        in operator_report["known_gaps"]
    )
    workerbee_status = json.loads(
        (run_dir / "workerbee-status.json").read_text(encoding="utf-8")
    )
    assert workerbee_status["api_version"] == "workerbee.mcp/v1"
    assert workerbee_status["kind"] == "ProjectStatus"
    assert workerbee_status["ok"] is None


def test_ai_fabric_lab_acceptance_closeout_copies_workerbee_status(
    tmp_path: Path, monkeypatch
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_acceptance_closeout_status_test")
    storage_root = tmp_path / "lab"
    adapter = storage_root / "adapters" / "expert" / "validation"
    adapter.mkdir(parents=True)
    (adapter / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": "Qwen/Qwen2.5-Coder-7B-Instruct",
                "r": 16,
                "target_modules": ["q_proj", "k_proj"],
            }
        ),
        encoding="utf-8",
    )
    (adapter / "adapter_model.safetensors").write_text("", encoding="utf-8")
    status_path = tmp_path / "project-status.json"
    status_path.write_text(
        json.dumps(
            {
                "api_version": "workerbee.mcp/v1",
                "kind": "ProjectStatus",
                "ok": True,
                "data": {"app_status": {"ready": True, "ready_workload_count": 8}},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        lab,
        "_health_snapshot",
        lambda **kwargs: {
            "ok": True,
            "checked_at": "2026-06-02T00:00:00+00:00",
            "endpoints": {
                "router": {"ok": True, "service": "ai-router"},
                "das": {
                    "ok": True,
                    "service": "das-bridge",
                    "fact_count": 12,
                    "f5_evidence_count": 7,
                },
                "retrieval": {
                    "ok": True,
                    "service": "retrieval-indexer",
                    "document_count": 3,
                    "chunk_count": 9,
                },
            },
        },
    )
    monkeypatch.setattr(
        lab,
        "_host_alias_snapshot",
        lambda **kwargs: {"ok": True, "checked_at": "2026-06-02T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        lab,
        "_lane_readiness_snapshot",
        lambda **kwargs: {
            "ok": True,
            "lanes": {
                "coordinator": {"ok": True, "model_id": "general-coordinator"},
                "expert": {"ok": True, "model_id": "k1s-code-expert"},
            },
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_lora_adapter_smoke",
        lambda **kwargs: {
            "ok": True,
            "preflight": kwargs["preflight"],
            "adapter_model": "k1s-code-expert-lora-smoke",
            "base_model": "k1s-code-expert",
            "findings": [],
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_quality_comparison",
        lambda **kwargs: {"ok": True, "results": [], "findings": []},
    )
    monkeypatch.setattr(
        lab,
        "_run_stress_burst",
        lambda **kwargs: {
            "ok": True,
            "final_vram_growth_mib": 12,
            "request_count": 4,
            "ok_count": 4,
            "findings": [],
        },
    )
    monkeypatch.setattr(
        lab,
        "_run_recovery_smoke",
        lambda **kwargs: {"ok": True, "results": [], "findings": []},
    )
    monkeypatch.setattr(
        lab,
        "_run_advisor_scenarios",
        lambda **kwargs: {"ok": True, "scenario_count": 2, "findings": []},
    )
    monkeypatch.setattr(
        lab,
        "_run_evidence_closeout",
        lambda **kwargs: {"ok": True, "record_count": 3, "findings": []},
    )

    result = lab.validate_runtime(
        EXAMPLE_ROOT,
        suite="acceptance-closeout",
        prompts=None,
        storage_root=storage_root,
        run_id="acceptance-closeout-status-test",
        track=None,
        router_url="http://127.0.0.1:18180",
        das_url="http://127.0.0.1:18181",
        retrieval_url="http://127.0.0.1:18182",
        duration_seconds=None,
        workers=None,
        worker_sleep_seconds=0,
        gpu_sample_seconds=None,
        request_timeout=1,
        success_threshold=0.95,
        vram_growth_mib_max=4096,
        workerbee_status=status_path,
    )

    copied = json.loads(
        (Path(result["run_dir"]) / "workerbee-status.json").read_text(encoding="utf-8")
    )
    operator_report = json.loads(
        (Path(result["run_dir"]) / "operator-report.json").read_text(encoding="utf-8")
    )

    assert result["ok"] is True
    assert copied["api_version"] == "workerbee.mcp/v1"
    assert copied["kind"] == "ProjectStatus"
    assert copied["ok"] is True
    assert copied["source"] == "workerbee.mcp.project_status"
    assert copied["data"]["app_status"]["ready_workload_count"] == 8
    assert (
        "Final WorkerBee MCP project status should be refreshed in workerbee-status.json before promotion."
        not in operator_report["known_gaps"]
    )


def test_ai_fabric_lab_lane_readiness_retries_until_models_answer() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_lane_readiness_test")
    calls: list[str] = []

    def fake_post_json(url: str, payload: dict[str, object], timeout: int) -> dict[str, object]:
        del url, timeout
        lane = str(payload["lane"])
        calls.append(lane)
        if lane == "expert" and calls.count("expert") == 1:
            return {"status": 503, "error": "upstream unavailable"}
        return {
            "status": 200,
            "json": {
                "model": lab._expected_chat_model(lane),
                "choices": [{"message": {"content": "ok"}}],
            },
        }

    lab._post_json = fake_post_json

    result = lab._lane_readiness_snapshot(
        router_url="http://router.example",
        run_id="readiness-test",
        timeout_seconds=3,
        request_timeout=1,
        interval_seconds=0,
    )

    assert result["ok"] is True
    assert result["lanes"]["coordinator"]["attempts"] == 1
    assert result["lanes"]["expert"]["attempts"] == 2


def test_ai_fabric_lab_init_storage_can_target_temp_root(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "init-storage",
            "--storage-root",
            str(tmp_path / "lab"),
            "--json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(result.stdout)

    assert payload["ok"] is True
    assert (tmp_path / "lab" / "models" / "hf-cache").is_dir()
    assert (tmp_path / "lab" / "config" / "model-tracks.json").is_file()


def test_ai_fabric_lab_sync_corpus_can_target_temp_root(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "sync-corpus",
            "--storage-root",
            str(tmp_path / "lab"),
            "--k1s-root",
            str(tmp_path / "missing-k1s"),
            "--max-files",
            "12",
            "--json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(result.stdout)

    assert payload["ok"] is True
    assert payload["copied"]["workerbee"] > 0
    assert payload["copied"]["k1s"] == 0
    assert (tmp_path / "lab" / "corpus" / "workerbee").is_dir()


def test_ai_fabric_phase_report_facts_reflect_k1s_gate() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_phase_report_test")
    report = {
        "api_version": "k1s.fabric.phase-assurance/v1",
        "kind": "FabricPhaseAssuranceReport",
        "phase_order": ["F0", "F1", "F2", "F3"],
        "ready_phases": ["F0"],
        "phases": {
            "F0": {
                "status": "present",
                "present": ["inference_cell_ready"],
                "missing": [],
                "gate": {"ready": True, "blocked_by": []},
            },
            "F1": {
                "status": "missing",
                "present": ["typed_accelerators"],
                "missing": ["typed_link_topology"],
                "evidence": {
                    "typed_accelerators": {"accelerator_count": 2},
                    "typed_link_topology": False,
                },
                "gate": {"ready": False, "blocked_by": []},
            },
            "F2": {
                "status": "missing",
                "present": [],
                "missing": ["content_addressed_chunks"],
                "gate": {"ready": False, "blocked_by": ["F1"]},
            },
            "F3": {
                "status": "present",
                "present": ["advisory_contract"],
                "missing": [],
                "gate": {"ready": False, "blocked_by": ["F1", "F2"]},
            },
        },
    }

    facts = lab._phase_report_facts(report)

    assert {
        "namespace": "runtime",
        "subject": "k1s.fabric.phase.F3",
        "predicate": "blocked_by",
        "object": "F2",
        "source": "k1s.fabric.phase-assurance/v1",
    } in facts
    assert {
        "namespace": "runtime",
        "subject": "k1s.fabric.phase.F1.evidence.typed_link_topology",
        "predicate": "present",
        "object": False,
        "source": "k1s.fabric.phase-assurance/v1",
    } in facts
    assert {
        "namespace": "runtime",
        "subject": "k1s.fabric.phase_report",
        "predicate": "ready_phase",
        "object": "F0",
        "source": "k1s.fabric.phase-assurance/v1",
    } in facts
    assert {
        "namespace": "runtime",
        "subject": "k1s.fabric.phase.F1.evidence.typed_accelerators",
        "predicate": "detail.accelerator_count",
        "object": 2,
        "source": "k1s.fabric.phase-assurance/v1",
    } in facts


def test_ai_fabric_import_phase_facts_posts_report_facts(tmp_path: Path, monkeypatch) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_phase_import_test")
    phase_report = tmp_path / "phase-report.json"
    phase_report.write_text(
        json.dumps(
            {
                "api_version": "k1s.fabric.phase-assurance/v1",
                "kind": "FabricPhaseAssuranceReport",
                "phase_order": ["F0"],
                "ready_phases": ["F0"],
                "phases": {
                    "F0": {
                        "status": "present",
                        "present": ["inference_cell_ready"],
                        "missing": [],
                        "gate": {"ready": True, "blocked_by": []},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    posted: list[dict[str, object]] = []

    def fake_post(_das_url: str, fact: dict[str, object]) -> dict[str, object]:
        posted.append(fact)
        return {"ok": True}

    monkeypatch.setattr(lab, "_post_das_fact", fake_post)

    result = lab.import_phase_facts(phase_report=phase_report, das_url="http://das.local")

    assert result["ok"] is True
    assert result["posted"] == [{"ok": True} for _ in posted]
    assert posted
    assert posted[0]["source"] == "k1s.fabric.phase-assurance/v1"


def test_ai_fabric_emit_f5_evidence_writes_k1s_compatible_records(tmp_path: Path) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_f5_evidence_test")

    result = lab.emit_f5_evidence(
        EXAMPLE_ROOT,
        storage_root=tmp_path / "lab",
        site_id="site-a",
        peer_site_id="site-b",
        project="k1s-workerbee-test",
        track="smoke",
        query_id="query-0",
    )

    payload = json.loads(Path(result["evidence_path"]).read_text(encoding="utf-8"))
    records = payload["records"]
    assert result["ok"] is True
    assert payload["api_version"] == "workerbee.ai-fabric.f5-evidence/v1"
    assert records["das_cell_bundles"][0]["site_id"] == "site-a"
    assert records["das_query_traces"][0]["local_first"] is True
    assert records["das_replications"][0]["mode"] == "controlled"
    assert records["cognitive_signals"][0]["review_gate"] == "operator_review"
    assert {
        "namespace": "runtime",
        "subject": "k1s.fabric.phase.F5.evidence.das_cell_bundles",
        "predicate": "workerbee_record_count",
        "object": 1,
        "source": "workerbee.ai-fabric.f5-evidence/v1",
    } in result["facts"]


def test_ai_fabric_advisory_prompt_record_carries_k1s_import_payload(monkeypatch) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_k1s_import_record_test")

    def fake_post_json(
        url: str,
        payload: dict[str, object],
        *,
        timeout: int,
    ) -> dict[str, object]:
        del url, payload, timeout
        return {
            "ok": True,
            "status": 200,
            "json": {
                "ok": True,
                "lane": "coordinator",
                "authoritative": False,
                "trace_id": "trace-import-0",
                "trace_path": "/srv/storage/traces/trace-import-0.json",
                "model": {"ok": True, "raw": {"model": "general-coordinator"}},
                "evidence": {
                    "retrieval": {"results": [{"path": "workerbee/notes.md"}]},
                    "symbolic": {"results": [{"id": "das-fact://fact-1"}]},
                },
                "decision_trace": {
                    "trace_id": "trace-import-0",
                    "request_id": "req-import-0",
                    "selected_lane": "coordinator",
                    "request_contract": {
                        "subject_type": "advisory_query",
                        "subject_id": "test",
                        "intent": "advise",
                    },
                    "response_contract": {
                        "provider": "workerbee-ai-router",
                        "status": "ok",
                        "recommendation": "review",
                        "authoritative": False,
                    },
                    "retrieval": {"results": [{"path": "workerbee/notes.md"}]},
                    "symbolic": {"results": [{"id": "das-fact://fact-1"}]},
                },
            },
        }

    monkeypatch.setattr(lab, "_post_json", fake_post_json)

    record = lab._advisory_prompt_record(
        prompt={
            "id": "prompt-0",
            "prompt": "Explain k1s advisory routing",
            "lane": "coordinator",
        },
        router_url="http://router.example",
        run_id="run-0",
        request_timeout=1,
    )

    payload = record["k1s_advisory_import"]
    assert record["ok"] is True
    assert payload["api_version"] == lab.K1S_ADVISORY_IMPORT_API_VERSION
    assert payload["decision_traces"][0]["trace_id"] == "trace-import-0"


def test_ai_fabric_k1s_advisory_import_posts_traces_and_f5_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_k1s_import_post_test")
    f5_path = tmp_path / "f5-evidence.json"
    f5_path.write_text(
        json.dumps(
            {
                "ok": True,
                "records": [
                    {
                        "kind": "das_cell_bundle",
                        "payload": {
                            "bundle_id": "das-import-runtime",
                            "site_id": "site-a",
                            "cell_id": "runtime",
                            "version": "2026-06-04",
                            "storage_ref": "/srv/storage/k1s/ai-fabric-lab/das",
                            "facts_ref": "das://site-a/runtime/facts.jsonl",
                            "status": "ready",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_post_json(
        url: str,
        payload: dict[str, object],
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        calls.append({"url": url, "payload": payload, "timeout": timeout, "headers": headers})
        return {
            "ok": True,
            "status": 200,
            "json": {
                "ok": True,
                "imported_count": 2,
                "counts": {"decision_traces": 1, "das_cell_bundles": 1},
                "findings": [],
            },
        }

    monkeypatch.setattr(lab, "_post_json", fake_post_json)
    summary = {
        "run_id": "run-0",
        "suites": {
            "quality-contract": {
                "results": [
                    {
                        "k1s_advisory_import": {
                            "decision_traces": [
                                {
                                    "trace_id": "trace-import-0",
                                    "request_id": "req-import-0",
                                    "response_contract": {"authoritative": False},
                                }
                            ]
                        }
                    }
                ]
            }
        },
    }

    result = lab._import_k1s_advisory_state(
        summary=summary,
        f5_evidence_path=f5_path,
        k1s_url="http://127.0.0.1:19108",
        k1s_token="admin-token",  # noqa: S106 - dummy bearer token for request-header assertion.
        workerbee_status={},
        skip=False,
        timeout_seconds=7,
    )

    assert result["ok"] is True
    assert result["imported_count"] == 2
    assert calls[0]["url"] == "http://127.0.0.1:19108/fabric/advisory/import"
    assert calls[0]["headers"] == {"Authorization": "Bearer admin-token"}
    assert calls[0]["payload"]["decision_traces"][0]["trace_id"] == "trace-import-0"
    assert calls[0]["payload"]["records"][0]["kind"] == "das_cell_bundle"


def test_ai_fabric_f1_f2_locality_closeout_seeds_controller_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_f1_f2_closeout_test")
    post_calls: list[dict[str, object]] = []
    get_calls: list[str] = []

    def fake_post_json(
        url: str,
        payload: dict[str, object],
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        post_calls.append({"url": url, "payload": payload, "timeout": timeout, "headers": headers})
        assert url.endswith("/fabric/advisory/import")
        return {
            "ok": True,
            "status": 200,
            "json": {
                "ok": True,
                "imported_count": 4,
                "counts": {
                    "fabric_nodes": 1,
                    "fabric_chunks": 1,
                    "fabric_residencies": 1,
                    "fabric_movements": 1,
                },
                "findings": [],
            },
        }

    def fake_get_json(
        url: str,
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout, headers
        get_calls.append(url)
        if url.endswith("/nodes"):
            return {
                "ok": True,
                "count": 1,
                "nodes": [
                    {
                        "node_id": "node-a",
                        "name": "node-a",
                        "status": "Ready",
                        "labels": {
                            "gpu.present": "true",
                            "gpu.count": "1",
                            "gpu.models": "RTX 8000",
                        },
                        "capabilities": {
                            "accelerators": [
                                {
                                    "id": "gpu-0",
                                    "vendor": "nvidia",
                                    "family": "RTX 8000",
                                    "device_count": 1,
                                    "execution_role": "execution",
                                }
                            ]
                        },
                    }
                ],
            }
        if "/fabric/chunks" in url:
            return {"ok": True, "items": [{"chunk_id": "sha256:" + ("a" * 64)}], "count": 1}
        if url.endswith("/fabric/residencies"):
            return {"ok": True, "items": [{"node_id": "node-a"}], "count": 1}
        if url.endswith("/fabric/movements"):
            return {"ok": True, "items": [{"movement_id": "movement-0"}], "count": 1}
        if url.endswith("/fabric/advisory/state"):
            return {"ok": True, "mode": "advisory_only"}
        assert url.endswith("/fabric/phase-assurance")
        return {
            "api_version": "k1s.fabric.phase-assurance/v1",
            "kind": "FabricPhaseAssuranceReport",
            "source": "k1s-controller-state",
            "controller_authority": "k1s",
            "authoritative": True,
            "advisory_authoritative": False,
            "phases": {
                "F1": {
                    "status": "present",
                    "present": [
                        "typed_node_capabilities",
                        "typed_accelerators",
                        "typed_storage_media",
                        "typed_link_topology",
                        "typed_rnic_rdma",
                        "identity_role_separation",
                        "gpu_label_projection",
                    ],
                    "missing": [],
                    "gate": {"ready": False, "blocked_by": ["F0"]},
                },
                "F2": {
                    "status": "present",
                    "present": [
                        "content_addressed_chunks",
                        "residency_state",
                        "controlled_push_pull",
                        "integrity_epoch_semantics",
                    ],
                    "missing": [],
                    "gate": {"ready": True, "blocked_by": []},
                },
                "F3": {
                    "status": "present",
                    "present": ["advisory_contract"],
                    "missing": [],
                    "gate": {"ready": True, "blocked_by": []},
                },
            },
        }

    monkeypatch.setattr(lab, "_post_json", fake_post_json)
    monkeypatch.setattr(lab, "_get_json", fake_get_json)
    workerbee_status = {
        "api_version": "workerbee.mcp/v1",
        "kind": "ProjectStatus",
        "ok": True,
        "data": {
            "stack": {
                "controller_url": "http://127.0.0.1:19108",
                "service_ports": {"api": 25971},
            }
        },
    }
    status_path = tmp_path / "workerbee-status.json"
    status_path.write_text(json.dumps(workerbee_status), encoding="utf-8")

    result = lab.closeout_f1_f2_locality(
        EXAMPLE_ROOT,
        storage_root=tmp_path / "storage",
        run_id="f1-f2-closeout-test",
        project="workerbee-project",
        workerbee_status=status_path,
        k1s_url="",
        k1s_token="admin-token",  # noqa: S106 - dummy bearer token for request-header assertion.
        request_timeout=3,
    )

    assert result["api_version"] == lab.F1_F2_LOCALITY_CLOSEOUT_API_VERSION
    assert result["ok"] is True
    assert result["node_id"] == "node-a"
    assert result["checks"]["f1_node_imported"] is True
    assert result["checks"]["f1_evidence_present"] is True
    assert result["checks"]["f2_evidence_present"] is True
    assert result["k1s_phase_assurance"]["f3_gate_ready"] is True
    assert Path(result["artifacts"]["f1-f2-locality-closeout-summary.json"]).is_file()
    assert post_calls[0]["url"] == "http://127.0.0.1:19108/fabric/advisory/import"
    assert post_calls[0]["headers"] == {"Authorization": "Bearer admin-token"}
    records = post_calls[0]["payload"]["records"]
    assert records["fabric_nodes"][0]["capabilities"]["storage_devices"][0]["medium"] == "nvme"
    assert records["fabric_nodes"][0]["capabilities"]["identity_roles"]["fabric"].endswith("/fabric")
    assert records["fabric_chunks"][0]["namespace"] == "ai-fabric-lab"
    assert records["fabric_residencies"][0]["node_id"] == "node-a"
    assert records["fabric_movements"][0]["direction"] == "pull"
    assert "http://127.0.0.1:19108/nodes" in get_calls


def test_ai_fabric_f3_advisory_closeout_writes_phase_assurance_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_f3_closeout_test")
    phase_report = tmp_path / "phase-report.json"
    phase_report.write_text(
        json.dumps(
            {
                "api_version": "k1s.fabric.phase-assurance/v1",
                "kind": "FabricPhaseAssuranceReport",
                "phase_order": ["F3"],
                "ready_phases": [],
                "phases": {
                    "F3": {
                        "phase": "F3",
                        "status": "missing",
                        "present": [],
                        "missing": ["bounded_planning"],
                        "evidence": {"bounded_planning": False},
                        "gate": {"ready": False, "blocked_by": ["F1", "F2"]},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    post_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        lab,
        "_resolve_runtime_endpoints",
        lambda **kwargs: {
            "router_url": kwargs["router_url"],
            "das_url": kwargs["das_url"],
            "retrieval_url": kwargs["retrieval_url"],
        },
    )
    monkeypatch.setattr(
        lab,
        "_health_snapshot",
        lambda **kwargs: {"ok": True, "checked_at": "2026-06-04T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        lab,
        "_host_alias_snapshot",
        lambda **kwargs: {"ok": True, "endpoints": {}, "checked_at": "2026-06-04T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        lab,
        "import_runtime_facts",
        lambda *args, **kwargs: {
            "ok": True,
            "das_url": kwargs["das_url"],
            "track": kwargs["track"],
            "facts": [],
            "posted": {"ok": True},
            "findings": [],
        },
    )

    def fake_post_json(
        url: str,
        payload: dict[str, object],
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        post_calls.append({"url": url, "payload": payload, "timeout": timeout, "headers": headers})
        if url.endswith("/v1/advisory/query"):
            return {
                "ok": True,
                "status": 200,
                "json": {
                    "ok": True,
                    "lane": "expert",
                    "authoritative": False,
                    "trace_id": "trace-f3-closeout",
                    "decision_trace": {
                        "trace_id": "trace-f3-closeout",
                        "request_id": "req-f3-closeout",
                        "request_contract": {
                            "subject_type": "k1s_fabric_phase",
                            "subject_id": "k1s.fabric.phase.F3",
                            "intent": "review_phase_gate",
                            "facts_ref": "http://das.example",
                            "locality_snapshot_ref": "http://retrieval.example",
                            "max_candidates": 5,
                            "time_budget_ms": 3000,
                        },
                        "response_contract": {
                            "provider": "workerbee-ai-router",
                            "status": "ok",
                            "recommendation": "review F3 evidence",
                            "authoritative": False,
                        },
                        "deterministic_baseline": {"selected_lane": "expert"},
                        "accepted": None,
                        "divergence_reason": "pending_operator_review",
                        "replay_status": "recorded",
                        "continuity_signals": {"request_id": "req-f3-closeout"},
                        "coherence_signals": {"model_ok": True},
                    },
                },
            }
        assert url.endswith("/fabric/advisory/import")
        return {
            "ok": True,
            "status": 200,
            "json": {
                "ok": True,
                "imported_count": 4,
                "counts": {"decision_traces": 1, "das_cell_bundles": 1},
                "findings": [],
            },
        }

    def fake_f5_closeout(*, das_url: str, f5_evidence_path: Path) -> dict[str, object]:
        del das_url
        f5_evidence_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "records": [
                        {
                            "kind": "das_cell_bundle",
                            "payload": {
                                "bundle_id": "das-f3",
                                "site_id": "site-a",
                                "storage_ref": "/srv/das",
                                "facts_ref": "das://facts",
                                "status": "ready",
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return {"ok": True, "record_count": 1, "kinds": ["das_cell_bundle"], "findings": []}

    def fake_get_json(
        url: str,
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout, headers
        if url.endswith("/fabric/advisory/state"):
            return {
                "ok": True,
                "mode": "advisory_only",
                "authoritative": False,
                "controller_authority": "k1s",
                "experimental_providers": ["hyperon-das"],
                "advisory": {
                    "traces_count": 1,
                    "pending_review_count": 1,
                    "latest_trace": {"trace_id": "trace-f3-closeout"},
                    "traces": [{"trace_id": "trace-f3-closeout"}],
                },
            }
        assert url.endswith("/fabric/phase-assurance")
        return {
            "api_version": "k1s.fabric.phase-assurance/v1",
            "kind": "FabricPhaseAssuranceReport",
            "source": "k1s-controller-state",
            "controller_authority": "k1s",
            "authoritative": True,
            "advisory_authoritative": False,
            "phases": {
                "F3": {
                    "status": "present",
                    "present": [
                        "advisory_contract",
                        "decision_traces",
                        "divergence_logging",
                        "replay_evaluation",
                        "bounded_planning",
                        "continuity_coherence_signals",
                    ],
                    "missing": [],
                    "gate": {"ready": False, "blocked_by": ["F1", "F2"]},
                }
            },
        }

    monkeypatch.setattr(lab, "_post_json", fake_post_json)
    monkeypatch.setattr(lab, "_run_evidence_closeout", fake_f5_closeout)
    monkeypatch.setattr(lab, "_get_json", fake_get_json)

    result = lab.closeout_f3_advisory(
        EXAMPLE_ROOT,
        stage=EXAMPLE_ROOT / "stage-quality",
        storage_root=tmp_path / "storage",
        run_id="f3-closeout-test",
        track="quality",
        router_url="http://router.example",
        das_url="http://das.example",
        retrieval_url="http://retrieval.example",
        project="workerbee-project",
        k1s_root=tmp_path,
        phase_report=phase_report,
        workerbee_status=None,
        k1s_url="http://127.0.0.1:19108",
        k1s_token="admin-token",  # noqa: S106 - dummy bearer token for request-header assertion.
        request_timeout=3,
    )

    assert result["api_version"] == lab.F3_ADVISORY_CLOSEOUT_API_VERSION
    assert result["ok"] is True
    assert result["trace_id"] == "trace-f3-closeout"
    assert result["checks"]["f3_evidence_present"] is True
    assert result["checks"]["phase_controller_authority"] is True
    assert result["k1s_phase_assurance"]["f3_status"] == "present"
    assert result["k1s_phase_assurance"]["f3_gate_ready"] is False
    assert result["k1s_phase_assurance"]["f3_blocked_by"] == ["F1", "F2"]
    assert Path(result["artifacts"]["f3-advisory-closeout-summary.json"]).is_file()
    assert Path(result["artifacts"]["k1s-phase-assurance.json"]).is_file()
    assert post_calls[0]["url"] == "http://router.example/v1/advisory/query"
    assert post_calls[0]["payload"]["subject_id"] == "k1s.fabric.phase.F3"
    assert post_calls[1]["url"] == "http://127.0.0.1:19108/fabric/advisory/import"
    assert post_calls[1]["headers"] == {"Authorization": "Bearer admin-token"}


def test_ai_fabric_retrieval_indexer_serves_local_results(tmp_path: Path, monkeypatch) -> None:
    indexer = _load_module(
        EXAMPLE_ROOT / "images" / "retrieval-indexer" / "indexer.py",
        "ai_fabric_retrieval_indexer_test",
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    workerbee = corpus / "workerbee"
    workerbee.mkdir()
    (workerbee / "notes.md").write_text(
        "WorkerBee fabric controller evidence for k1s advisory retrieval.",
        encoding="utf-8",
    )

    monkeypatch.setattr(indexer, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(indexer, "ARTIFACTS_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(indexer, "QDRANT_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(indexer, "QDRANT_TIMEOUT", 0.2)

    result = indexer.build_index()
    search = indexer.search("workerbee fabric controller", limit=1)

    assert result["ok"] is True
    assert result["document_count"] == 1
    assert result["chunk_count"] == 1
    assert search["ok"] is True
    assert search["backend"] == "local"
    assert search["results"][0]["path"] == "workerbee/notes.md"


def test_ai_fabric_router_advisory_includes_retrieval_and_lane_override(
    tmp_path: Path, monkeypatch
) -> None:
    router = _load_module(
        EXAMPLE_ROOT / "images" / "router" / "app.py",
        "ai_fabric_router_test",
    )

    def fake_retrieve(query: str, *, limit: int) -> dict[str, object]:
        assert limit == 5
        return {
            "ok": True,
            "query": query,
            "backend": "local",
            "results": [{"path": "workerbee/notes.md", "score": 1.0, "text": "evidence"}],
        }

    def fake_model(
        lane: str,
        payload: dict[str, object],
        retrieval: dict[str, object],
        symbolic: dict[str, object],
        *,
        advisory_decision: dict[str, object],
    ) -> dict[str, object]:
        assert advisory_decision["decision"]["status"] == "review"
        return {
            "ok": False,
            "lane": lane,
            "error": (
                f"model disabled for {payload['query']} with {len(retrieval['results'])} "
                f"retrieval hit and {len(symbolic['results'])} symbolic hit"
            ),
        }

    monkeypatch.setattr(router, "_retrieve_evidence", fake_retrieve)
    monkeypatch.setattr(
        router,
        "_query_symbolic_evidence",
        lambda query, *, limit: {"ok": True, "results": [{"subject": query, "limit": limit}]},
    )

    def fake_das_decision(
        *,
        payload: dict[str, object],
        query: str,
        symbolic: dict[str, object],
        limit: int,
    ) -> dict[str, object]:
        assert symbolic["results"][0]["limit"] == limit
        return {
            "ok": True,
            "api_version": "workerbee.ai-fabric.advisory-decision/v1",
            "decision": {
                "api_version": "workerbee.ai-fabric.advisory-decision/v1",
                "decision_id": "advisory-decision-test",
                "subject": query,
                "intent": payload.get("intent") or "advise",
                "status": "review",
                "recommended_action": "review with symbolic evidence",
                "confidence": 0.7,
                "evidence_refs": ["das-fact://fact-1"],
                "risks": ["relationship_context_sparse"],
                "blocked_conditions": [],
                "authoritative": False,
            },
        }

    monkeypatch.setattr(
        router,
        "_query_das_advisory_decision",
        fake_das_decision,
    )
    monkeypatch.setattr(router, "_call_advisory_model", fake_model)
    monkeypatch.setattr(router, "TRACE_DIR", tmp_path)

    response = router._advisory_response(
        {"lane": "coordinator", "query": "python k1s traceback should not force expert"}
    )
    trace_path = tmp_path / f"{response['trace_id']}.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))

    assert response["ok"] is True
    assert response["authoritative"] is False
    assert response["lane"] == "coordinator"
    assert response["trace_path"] == str(trace_path)
    assert response["evidence"]["retrieval"]["results"][0]["path"] == "workerbee/notes.md"
    assert response["evidence"]["symbolic"]["results"][0]["limit"] == 5
    assert response["advisory_decision"]["decision"]["decision_id"] == "advisory-decision-test"
    assert response["model"]["lane"] == "coordinator"
    assert trace["authoritative"] is False
    assert trace["controller_authority"] == "k1s"
    assert trace["request_contract"]["max_candidates"] == 5
    assert trace["response_contract"]["authoritative"] is False
    assert trace["evidence_contract"]["retrieval_required"] is True
    assert trace["evidence_contract"]["symbolic_required"] is True
    assert trace["evidence_contract"]["das_decision_required"] is True
    assert trace["evidence_contract"]["das_decision_ok"] is True
    assert trace["evidence_contract"]["das_decision_evidence_count"] == 1
    assert trace["evidence_contract"]["retrieval_result_count"] == 1
    assert trace["evidence_contract"]["symbolic_result_count"] == 1
    assert trace["retrieval"]["results"][0]["path"] == "workerbee/notes.md"
    assert trace["symbolic"]["results"][0]["subject"] == trace["query"]
    assert trace["advisory_decision"]["decision"]["authoritative"] is False
    assert trace["replay_status"] == "recorded"
    assert trace["divergence_reason"] == "pending_operator_review"


def test_ai_fabric_router_models_response_proxies_lane_models(monkeypatch) -> None:
    router = _load_module(
        EXAMPLE_ROOT / "images" / "router" / "app.py",
        "ai_fabric_router_models_test",
    )

    def fake_get_json(url: str, *, timeout: float) -> dict[str, object]:
        assert timeout == router.PROXY_TIMEOUT
        if "ai-expert" in url:
            return {
                "object": "list",
                "data": [
                    {"id": "k1s-code-expert"},
                    {"id": "k1s-code-expert-lora-smoke"},
                ],
            }
        return {"object": "list", "data": [{"id": "general-coordinator"}]}

    monkeypatch.setattr(router, "_get_json", fake_get_json)

    payload, status = router._models_response(lane="expert")

    assert status == 200
    assert payload["ok"] is True
    assert [item["id"] for item in payload["data"]] == [
        "k1s-code-expert",
        "k1s-code-expert-lora-smoke",
    ]
    assert "expert" in payload["lanes"]


def test_ai_fabric_fake_model_returns_openai_chat_completion() -> None:
    fake_model = _load_module(
        EXAMPLE_ROOT / "images" / "fake-model" / "app.py",
        "ai_fabric_fake_model_test",
    )

    response = fake_model._chat_completion(
        {
            "model": "fake-expert",
            "messages": [
                {"role": "system", "content": "You are a test model."},
                {"role": "user", "content": "Explain k1s Hyperon advisory routing."},
            ],
        }
    )

    assert response["object"] == "chat.completion"
    assert response["model"] == "fake-expert"
    assert response["choices"][0]["message"]["role"] == "assistant"
    assert "router, retrieval, DAS, and trace plumbing" in response["choices"][0]["message"][
        "content"
    ]
    assert response["usage"]["total_tokens"] > 0


def test_ai_fabric_das_bridge_records_and_queries_facts(tmp_path: Path, monkeypatch) -> None:
    das_bridge = _load_module(
        EXAMPLE_ROOT / "images" / "das-bridge" / "app.py",
        "ai_fabric_das_bridge_test",
    )
    monkeypatch.setattr(das_bridge, "DATA_DIR", tmp_path)
    monkeypatch.setattr(das_bridge, "FACT_LOG", tmp_path / "facts.jsonl")

    fact = das_bridge._append_fact(
        {
            "namespace": "runtime",
            "subject": "ai_fabric.track",
            "predicate": "configured_as",
            "object": "smoke",
            "source": "test",
        }
    )
    das_bridge._append_fact(
        {
            "namespace": "runtime",
            "subject": "ai_fabric.symbolic_bridge",
            "predicate": "validated_by",
            "object": "newer-noisy-probe",
            "source": "test",
        }
    )
    project = das_bridge._append_fact(
        {
            "namespace": "runtime",
            "subject": "workerbee.project",
            "predicate": "name",
            "object": "k1s-workerbee-dev-test",
            "source": "test",
        }
    )
    results = das_bridge._query_facts(
        {"query": "ai_fabric track smoke workerbee project", "limit": 3}
    )

    assert fact["id"]
    assert fact["namespace"] == "runtime"
    assert results[0]["subject"] == "ai_fabric.track"
    assert results[0]["object"] == "smoke"
    assert results[1]["id"] == project["id"]


def test_ai_fabric_das_bridge_exposes_relationship_vocabulary() -> None:
    das_bridge = _load_module(
        EXAMPLE_ROOT / "images" / "das-bridge" / "app.py",
        "ai_fabric_das_bridge_relationships_test",
    )

    assert das_bridge.RELATIONSHIP_PREDICATES == (
        "owns_service",
        "depends_on",
        "serves_model",
        "requires_resource",
        "produced_artifact",
        "supports_advisory",
    )
    assert das_bridge.ADVISORY_DECISION_API_VERSION == (
        "workerbee.ai-fabric.advisory-decision/v1"
    )


def test_ai_fabric_das_bridge_builds_advisory_decision_from_runtime_facts(
    tmp_path: Path, monkeypatch
) -> None:
    das_bridge = _load_module(
        EXAMPLE_ROOT / "images" / "das-bridge" / "app.py",
        "ai_fabric_das_bridge_advisory_decision_test",
    )
    monkeypatch.setattr(das_bridge, "DATA_DIR", tmp_path)
    monkeypatch.setattr(das_bridge, "FACT_LOG", tmp_path / "facts.jsonl")
    monkeypatch.setattr(das_bridge, "F5_EVIDENCE_LOG", tmp_path / "f5-evidence.jsonl")

    dependency = das_bridge._append_fact(
        {
            "namespace": "runtime",
            "subject": "ai_fabric.service.ai-router",
            "predicate": "depends_on",
            "object": "ai_fabric.service.das-bridge",
            "source": "test",
        }
    )
    degraded = das_bridge._append_fact(
        {
            "namespace": "runtime",
            "subject": "ai_fabric.service.das-bridge",
            "predicate": "readiness",
            "object": "degraded",
            "source": "test",
        }
    )

    decision = das_bridge._advisory_decision(
        {
            "subject": "ai_fabric.service.ai-router",
            "intent": "advise",
            "query": "should ai-router handle symbolic evidence",
            "facts": [dependency, degraded],
        }
    )

    assert decision["api_version"] == "workerbee.ai-fabric.advisory-decision/v1"
    assert decision["authoritative"] is False
    assert decision["controller_authority"] == "k1s"
    assert decision["status"] == "blocked"
    assert decision["evidence_refs"] == [
        f"das-fact://{dependency['id']}",
        f"das-fact://{degraded['id']}",
    ]
    assert decision["blocked_conditions"][0]["condition"] == (
        "ai_fabric.service.das-bridge.readiness"
    )
    assert "symbolic_blocked_condition" in decision["risks"]

    artifact_decision = das_bridge._advisory_decision(
        {
            "subject": "ai_fabric.runtime_validation",
            "intent": "validate_artifacts",
            "query": "are runtime validation artifacts current",
            "facts": [
                {
                    "namespace": "runtime",
                    "subject": "ai_fabric.runtime_validation",
                    "predicate": "artifact_state",
                    "object": {"path": "runs/latest/summary.json", "state": "stale"},
                    "source": "test",
                }
            ],
        }
    )
    assert artifact_decision["status"] == "blocked"
    assert "validation_artifact_unhealthy" in artifact_decision["risks"]
    assert artifact_decision["blocked_conditions"][0]["condition"] == (
        "ai_fabric.runtime_validation.artifact_state"
    )

    phase_decision = das_bridge._advisory_decision(
        {
            "subject": "k1s.fabric.phase.F3",
            "intent": "review_phase_gate",
            "query": "can F3 proceed",
            "facts": [
                {
                    "namespace": "runtime",
                    "subject": "k1s.fabric.phase.F3",
                    "predicate": "gate_ready",
                    "object": False,
                    "source": "test",
                },
                {
                    "namespace": "runtime",
                    "subject": "k1s.fabric.phase.F3",
                    "predicate": "blocked_by",
                    "object": "F1",
                    "source": "test",
                },
                {
                    "namespace": "runtime",
                    "subject": "k1s.fabric.phase.F3.evidence.advisory_contract",
                    "predicate": "present",
                    "object": False,
                    "source": "test",
                },
            ],
        }
    )
    assert phase_decision["status"] == "blocked"
    assert "fabric_phase_gate_blocked" in phase_decision["risks"]
    assert "missing_phase_evidence" in phase_decision["risks"]
    assert phase_decision["blocked_conditions"][0]["condition"] == (
        "k1s.fabric.phase.F3.gate_ready"
    )
    assert "blocked_by=F1" in phase_decision["blocked_conditions"][0]["reason"]

    phase_report_decision = das_bridge._advisory_decision(
        {
            "subject": "k1s.fabric.phase_report",
            "intent": "validate_phase_report",
            "query": "is the phase report current",
            "facts": [
                {
                    "namespace": "runtime",
                    "subject": "k1s.fabric.phase_report",
                    "predicate": "artifact_state",
                    "object": {"path": "runs/fabric-phase-report.json", "state": "stale"},
                    "source": "test",
                }
            ],
        }
    )
    assert phase_report_decision["status"] == "blocked"
    assert "phase_report_stale" in phase_report_decision["risks"]
    assert "validation_artifact_unhealthy" in phase_report_decision["risks"]

    adapter_decision = das_bridge._advisory_decision(
        {
            "subject": "ai_fabric.adapter.k1s-code-expert-lora-smoke",
            "intent": "validate_lora_adapter",
            "query": "is the adapter ready",
            "facts": [
                {
                    "namespace": "runtime",
                    "subject": "ai_fabric.adapter.k1s-code-expert-lora-smoke",
                    "predicate": "adapter_state",
                    "object": {"state": "invalid"},
                    "source": "test",
                }
            ],
        }
    )
    assert adapter_decision["status"] == "blocked"
    assert "lora_adapter_not_ready" in adapter_decision["risks"]

    isolated_decision = das_bridge._advisory_decision(
        {
            "subject": "ai_fabric.service.ai-router",
            "intent": "advise",
            "query": "ignore stored facts for scenario evaluation",
            "facts": [],
            "use_stored_facts": False,
        }
    )
    assert isolated_decision["status"] == "review"
    assert isolated_decision["evidence_refs"] == []
    assert "missing_symbolic_evidence" in isolated_decision["risks"]


def test_ai_fabric_das_bridge_records_f5_query_evidence(tmp_path: Path, monkeypatch) -> None:
    das_bridge = _load_module(
        EXAMPLE_ROOT / "images" / "das-bridge" / "app.py",
        "ai_fabric_das_bridge_f5_test",
    )
    monkeypatch.setattr(das_bridge, "DATA_DIR", tmp_path)
    monkeypatch.setattr(das_bridge, "FACT_LOG", tmp_path / "facts.jsonl")
    monkeypatch.setattr(das_bridge, "F5_EVIDENCE_LOG", tmp_path / "f5-evidence.jsonl")

    fact = das_bridge._append_fact(
        {
            "namespace": "runtime",
            "subject": "k1s.fabric.phase.F5",
            "predicate": "status",
            "object": "present",
            "source": "test",
        }
    )
    evidence = das_bridge._record_query_evidence(
        {"query": "F5 DAS local first", "query_id": "query-0"},
        [fact],
    )
    records = das_bridge._read_f5_evidence()

    assert evidence["api_version"] == "workerbee.ai-fabric.f5-query-evidence/v1"
    assert evidence["query_trace"]["local_first"] is True
    assert evidence["query_trace"]["warmed_refs"] == [f"das-fact://{fact['id']}"]
    assert evidence["cognitive_signal"]["review_gate"] == "operator_review"
    assert [record["kind"] for record in records] == [
        "das_cell_bundle",
        "das_query_trace",
        "cognitive_signal",
    ]


def test_ai_fabric_lora_readiness_artifacts_are_valid() -> None:
    readiness = json.loads(
        (EXAMPLE_ROOT / "lora-readiness.json").read_text(encoding="utf-8")
    )
    prompt_path = EXAMPLE_ROOT / readiness["eval_prompts"]
    prompts = [
        json.loads(line)
        for line in prompt_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert readiness["api_version"] == "workerbee.ai-fabric.lora-readiness/v1"
    assert readiness["adapter_target"]["served_model_name"] == "k1s-code-expert"
    assert readiness["training_scope"]["status"] == "deferred"
    assert readiness["corpus_manifest_shape"]["api_version"] == (
        "workerbee.ai-fabric.lora-corpus/v1"
    )
    assert len(prompts) >= 6
    assert {item["lane"] for item in prompts} == {"expert"}
    assert all(item["min_retrieval_hits"] >= 1 for item in prompts)
    assert all(item["min_symbolic_facts"] >= 1 for item in prompts)
