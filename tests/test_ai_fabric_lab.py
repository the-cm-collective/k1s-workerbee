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


def test_ai_fabric_lab_runtime_suite_contract() -> None:
    lab = _load_module(SCRIPT, "ai_fabric_lab_runtime_suite_test")

    assert "adapter-preflight" in lab.RUNTIME_SUITE_CHOICES
    assert "lora-adapter-smoke" in lab.RUNTIME_SUITE_CHOICES
    assert "quality-comparison" in lab.RUNTIME_SUITE_CHOICES
    assert "stress-burst" in lab.RUNTIME_SUITE_CHOICES
    assert "recovery-smoke" in lab.RUNTIME_SUITE_CHOICES
    assert lab._selected_runtime_suites("all") == [
        "quality-contract",
        "mixed-soak",
        "evidence-closeout",
    ]
    assert lab._runtime_defaults_for_suite(
        suite="stress-burst",
        duration_seconds=None,
        workers=None,
        gpu_sample_seconds=None,
    ) == {"duration_seconds": 900, "workers": 6, "gpu_sample_seconds": 15}


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
        "requests.jsonl",
        "gpu-samples.jsonl",
        "health.json",
        "lane-readiness.json",
        "f5-evidence.json",
        "workerbee-status.json",
    }


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
    ) -> dict[str, object]:
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
    assert response["model"]["lane"] == "coordinator"
    assert trace["authoritative"] is False
    assert trace["controller_authority"] == "k1s"
    assert trace["request_contract"]["max_candidates"] == 5
    assert trace["response_contract"]["authoritative"] is False
    assert trace["retrieval"]["results"][0]["path"] == "workerbee/notes.md"
    assert trace["symbolic"]["results"][0]["subject"] == trace["query"]
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
