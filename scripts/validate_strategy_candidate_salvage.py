"""Regression: preserve supported LLM changes despite stale IDs and one bad evidence ref."""
import json
import pathlib
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import app  # noqa: E402


class FakeLLM:
    def chat(self, messages, **kwargs):
        return {
            "content": json.dumps({
                "analysis_summary": "保留有充分专家证据的增量变化。",
                "nodes": [{
                    "candidate_key": "node_new",
                    "change_type": "add",
                    "baseline_refs": [],
                    "node_name": "确认候选人关注点",
                    "reason": "专家明确说明先确认关注点。",
                    "evidence_refs": ["ev_good", "ev_missing"],
                    "context_refs": ["ev_context_missing"],
                    "script_evidence_refs": ["ev_good", "ev_script_missing"],
                    "is_fragment": False,
                }],
                "edges": [{
                    "candidate_key": "edge_new",
                    "change_type": "add",
                    "baseline_refs": [],
                    "from_ref": "ko_collision",
                    "to_ref": "node_new",
                    "condition": "候选人愿意继续说明关注点",
                    "reason": "专家给出了继续沟通的条件。",
                    "evidence_refs": ["ev_good", "ev_missing"],
                    "context_refs": [],
                }],
                "triggers": [{
                    "candidate_key": "trigger_new",
                    "change_type": "add",
                    "baseline_refs": [],
                    "condition": "需要回到基线开场",
                    "target_ref": "ko_collision",
                    "reason": "专家明确说明可重新确认来意。",
                    "evidence_refs": ["ev_good", "ev_missing"],
                    "context_refs": [],
                }],
                "candidate_states": [],
                "uncertainties": [],
            }, ensure_ascii=False),
            "model": "fake",
            "usage": {},
        }

    def load_config(self):
        return {"model": "fake", "max_utterances_per_call": 20}


with tempfile.TemporaryDirectory() as tmp:
    app.DB_PATH = str(pathlib.Path(tmp) / "db.json")
    created = app.create_task("candidate-salvage.txt", "hash-candidate-salvage")
    task, source = created["task"], created["source"]
    task_id = task["task_id"]
    source_id = source["source_id"]

    db = app._load_db()
    db["sources"][source_id].update(target_expert="Looper", baseline_id="base_current")
    next(item for item in db["tasks"] if item["task_id"] == task_id).update(
        target_expert="Looper", baseline_id="base_current", current_gate="G1"
    )
    db["gates"].append({"task_id": task_id, "gate_id": "G1", "decision": "approved"})
    db["evidence"] = [
        {
            "evidence_id": "ev_good", "utterance_id": "utt_good",
            "source_id": source_id, "task_id": task_id, "speaker": "Looper",
            "timestamp": "01:00", "content": "我会先确认他最关注的是什么。",
            "status": "candidate", "evidence_kind": "strategy",
        },
        {
            "evidence_id": "ev_other", "utterance_id": "utt_other",
            "source_id": source_id, "task_id": task_id, "speaker": "访谈者",
            "timestamp": "00:59", "content": "那你会先确认关注点吗？",
            "status": "candidate", "evidence_kind": "utterance",
        },
        {
            "evidence_id": "ev_foreign", "utterance_id": "utt_foreign",
            "source_id": "source_old", "task_id": "task_old", "speaker": "Looper",
            "timestamp": "01:01", "content": "另一任务里的同名专家证据。",
            "status": "candidate", "evidence_kind": "strategy",
        },
    ]
    baseline_graph = {
        "nodes": [
            {"id": "ko_collision", "label": "基线开场"},
            {"id": "base_target", "label": "基线下一步"},
        ],
        "edges": [], "triggers": [], "stop_conditions": [],
    }
    baseline = {
        "baseline_id": "base_current", "task_id": task_id, "source_id": source_id,
        "name": "current baseline", "version": 1, "graph": baseline_graph,
        "content_hash": app._stable_fingerprint(baseline_graph), "immutable": True,
    }
    db["graph_baselines"] = [baseline]
    # The same opaque ID exists globally as an old task object. Current-baseline
    # membership must win; otherwise valid imported endpoints become cross-group.
    db["knowledge"]["ko_collision"] = {
        "object_id": "ko_collision", "type": "strategy_node", "status": "candidate",
        "task_id": "task_old", "source_id": "source_old", "content": "old object",
        "evidence_refs": [], "linkage": {"group_id": "group_old"},
    }
    app._save_db(db)

    baseline_linkage = {
        "group_id": "group_current",
        "baseline_id": "base_current", "baseline_version": 1,
        "baseline_content_hash": baseline["content_hash"],
        "from_node_id": "ko_collision", "to_node_id": "base_target",
        "condition": "继续",
    }
    assert app.validate_knowledge_linkage(
        app._load_db(), "strategy_edge", baseline_linkage, task_id
    ) is None

    sanitized = app._sanitize_llm_evidence_refs(
        app._load_db(), app._load_db()["sources"][source_id],
        ["ev_good", "ev_missing", "ev_other", "ev_foreign"], ["ev_context_missing"],
    )
    assert sanitized["error"] is None
    assert sanitized["evidence_refs"] == ["ev_good"]
    assert sanitized["context_refs"] == ["ev_other"]
    assert {item["ref"] for item in sanitized["warnings"]} == {
        "ev_missing", "ev_other", "ev_foreign", "ev_context_missing",
    }
    wrong_only = app._sanitize_llm_evidence_refs(
        app._load_db(), app._load_db()["sources"][source_id], ["ev_other"], []
    )
    assert wrong_only["error"]["error"] == "target_expert_evidence_required"

    app.llm_client = FakeLLM()
    result = app.llm_extract_strategy(source_id, include_all=True)
    assert len(result["nodes_created"]) == 1, result
    assert len(result["edges"]) == 1, result
    assert len(result["triggers"]) == 1, result
    assert not result["rejected_nodes"], result["rejected_nodes"]
    assert not result["rejected_edges"], result["rejected_edges"]
    assert not result["rejected_triggers"], result["rejected_triggers"]
    assert result["evidence_ref_warnings"], result
    assert result["nodes_created"][0]["evidence_refs"] == ["ev_good"]

print(json.dumps({"status": "PASS", "contract": "candidate-salvage-v0.51"}, ensure_ascii=False))
