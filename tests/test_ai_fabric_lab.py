import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from workerbee.manifests import validate_stage

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
    assert "workerbee-ai-fabric-models:dev" in validation["images"]
    assert "ai-fabric-lab/ai-coordinator" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-expert" in validation["required_controller_scopes"]
    assert "ai-fabric-lab/ai-router" in validation["required_controller_scopes"]


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


def test_ai_fabric_router_advisory_includes_retrieval_and_lane_override(monkeypatch) -> None:
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

    response = router._advisory_response(
        {"lane": "coordinator", "query": "python k1s traceback should not force expert"}
    )

    assert response["ok"] is True
    assert response["authoritative"] is False
    assert response["lane"] == "coordinator"
    assert response["evidence"]["retrieval"]["results"][0]["path"] == "workerbee/notes.md"
    assert response["evidence"]["symbolic"]["results"][0]["limit"] == 5
    assert response["model"]["lane"] == "coordinator"


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
