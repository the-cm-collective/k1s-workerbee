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
    assert payload["tracks"] == ["baseline", "legacy-smollm-smoke", "quality", "smoke"]
    assert payload["stage"]["ok"] is True


def test_ai_fabric_lab_has_quality_track_with_qwen_coordinator() -> None:
    model_tracks = json.loads((EXAMPLE_ROOT / "model-tracks.json").read_text(encoding="utf-8"))
    smoke = model_tracks["tracks"]["smoke"]
    baseline = model_tracks["tracks"]["baseline"]
    quality = model_tracks["tracks"]["quality"]
    legacy = model_tracks["tracks"]["legacy-smollm-smoke"]

    assert model_tracks["run_defaults"]["attention_backend"] == "TRITON_ATTN"
    assert smoke["coordinator"]["model"] == "Qwen/Qwen2.5-3B-Instruct-AWQ"
    assert smoke["expert"]["model"] == "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"
    assert baseline["coordinator"]["model"] == "Qwen/Qwen2.5-7B-Instruct-AWQ"
    assert baseline["expert"]["model"] == "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
    assert quality["coordinator"]["model"] == "Qwen/Qwen2.5-7B-Instruct-AWQ"
    assert quality["expert"]["model"] == "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
    assert legacy["coordinator"]["model"] == "HuggingFaceTB/SmolLM3-3B"
    for track in model_tracks["tracks"].values():
        for lane in ("coordinator", "expert"):
            assert len(track[lane]["revision"]) == 40


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


def test_ai_fabric_lab_stages_use_dedicated_workerbee_service_ports() -> None:
    for stage in ("stage", "stage-baseline", "stage-plumbing"):
        manifests = EXAMPLE_ROOT / stage / "manifests"

        assert _service_port(manifests / "ai-router.yaml") == 18180
        assert _service_port(manifests / "das-bridge.yaml") == 18181
        assert _service_port(manifests / "retrieval-indexer.yaml") == 18182


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
