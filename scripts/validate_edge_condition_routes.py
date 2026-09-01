"""Isolated regression for human-calibrated edge conditions and shared route prompts."""
import hashlib
import json
import pathlib
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import app  # noqa: E402


class FakeLLM:
    def chat(self, messages, **kwargs):
        return {"content": "策略评价正文", "model": "fake", "usage": {}}

    def get_config_snapshot(self):
        return {"model": "fake"}


with tempfile.TemporaryDirectory() as tmp:
    app.DB_PATH = str(pathlib.Path(tmp) / "db.json")
    created = app.create_task("edge-routes.txt", "hash-edge-routes")
    task, source = created["task"], created["source"]
    db = app._load_db()
    db["sources"][source["source_id"]]["target_expert"] = "Looper"
    db["evidence"] = [{
        "evidence_id": "ev_route", "utterance_id": "utt_route",
        "source_id": source["source_id"], "task_id": task["task_id"],
        "speaker": "Looper", "timestamp": "12:34", "content": "这种情况下可以继续聊。",
        "status": "candidate", "evidence_kind": "strategy",
    }]
    baseline_graph = {
        "nodes": [
            {"id": "n1", "label": "开场"}, {"id": "n2", "label": "稍后联系"},
            {"id": "n3", "label": "继续沟通"}, {"id": "n4", "label": "结束"},
        ],
        "edges": [
            {"id": "e_missing", "source": "n1", "target": "n2", "label": ""},
            {"id": "e_fuzzy", "source": "n1", "target": "n3", "label": "对方似乎仍愿意交流"},
            {"id": "e_single", "source": "n2", "target": "n4", "label": ""},
        ],
        "triggers": [], "stop_conditions": [],
    }
    baseline = {
        "baseline_id": "base_route", "task_id": task["task_id"],
        "source_id": source["source_id"], "name": "route baseline", "version": 1,
        "graph": baseline_graph, "content_hash": app._stable_fingerprint(baseline_graph),
    }
    db["graph_baselines"] = [baseline]
    db["sources"][source["source_id"]]["baseline_id"] = "base_route"
    next(item for item in db["tasks"] if item["task_id"] == task["task_id"])["baseline_id"] = "base_route"
    db["knowledge"] = {
        "group_route": {
            "object_id": "group_route", "type": "strategy_script_group", "version": 1,
            "status": "candidate", "scope": "general", "content": "group",
            "evidence_refs": ["ev_route"], "linkage": {}, "task_id": task["task_id"],
            "source_id": source["source_id"], "immutable": False,
        },
        "edge_fuzzy": {
            "object_id": "edge_fuzzy", "type": "strategy_edge", "version": 1,
            "status": "candidate", "scope": "general", "content": "对方大致还有交流意愿",
            "evidence_refs": ["ev_route"], "task_id": task["task_id"],
            "source_id": source["source_id"], "immutable": False,
            "linkage": {
                "group_id": "group_route", "candidate_key": "edge_fuzzy_change",
                "change_type": "modify", "baseline_id": "base_route", "baseline_version": 1,
                "baseline_content_hash": baseline["content_hash"], "baseline_refs": ["e_fuzzy"],
                "from_ref": "n1", "to_ref": "n3", "from_node_id": "n1", "to_node_id": "n3",
                "condition": "对方大致还有交流意愿", "extracted_condition": "对方似乎仍愿意交流",
                "condition_uncertainty": "没有穷尽所有说法", "condition_review_status": "needs_review",
            },
        },
        "graph_route": {
            "object_id": "graph_route", "type": "graph", "version": 1,
            "status": "candidate", "scope": "general", "content": "route graph",
            "evidence_refs": ["ev_route"], "task_id": task["task_id"],
            "source_id": source["source_id"], "immutable": False,
            "linkage": {
                "group_id": "group_route", "baseline_id": "base_route",
                "baseline_version": 1, "baseline_content_hash": baseline["content_hash"],
                "node_ids": [], "edge_ids": ["edge_fuzzy"], "trigger_ids": [],
            },
        },
    }
    app._save_db(db)

    initial_hash = app._load_db()["graph_baselines"][0]["content_hash"]
    fuzzy = "候选人表达不完全明确，但结合上下文仍表现出继续交流的意愿"
    confirmed = app.save_edge_condition(
        task["task_id"], "graph_route", "candidate", "edge_fuzzy", fuzzy, "reviewer"
    )
    assert confirmed["current_condition"] == fuzzy
    assert confirmed["condition_review_status"] == "confirmed"
    assert confirmed["original_condition"] == "对方似乎仍愿意交流"

    baseline_fixed = app.save_edge_condition(
        task["task_id"], "graph_route", "baseline", "e_missing",
        "候选人当前不方便，倾向稍后再联系", "reviewer",
    )
    assert baseline_fixed["edge_origin"] == "candidate"
    db = app._load_db()
    assert db["graph_baselines"][0]["content_hash"] == initial_hash
    assert app._stable_fingerprint(db["graph_baselines"][0]["graph"]) == initial_hash

    materialized = app.materialize_incremental_graph(
        db, db["knowledge"]["graph_route"], app._graph_change_objects(db, db["knowledge"]["graph_route"]),
        db["graph_baselines"][0],
    )
    assert not materialized["condition_issues"], materialized["condition_issues"]
    assert any(edge.get("condition") == fuzzy for edge in materialized["edges"])
    assert next(edge for edge in materialized["edges"] if edge["id"] == "e_single")["condition_kind"] == "implicit_sequence"

    duplicate = app.classify_graph_edge_conditions({"nodes": [], "edges": [
        {"id": "d1", "source": "s", "target": "a", "label": "同一条件"},
        {"id": "d2", "source": "s", "target": "b", "label": "同一条件"},
    ]})[1]
    assert duplicate and duplicate[0]["error"] == "duplicate_branch_condition"
    assert app.save_edge_condition(task["task_id"], "graph_route", "candidate", "edge_fuzzy", "", "x")["error"] == "empty_edge_condition"
    assert app.get_edge_condition_workspace("other", "graph_route", "candidate", "edge_fuzzy")["error"] == "task_not_found"

    reviewed = app.review_graph_candidate(task["task_id"], "graph_route", "reviewer", "approved", "ok")
    assert "error" not in reviewed, reviewed
    assert app.save_edge_condition(task["task_id"], "graph_route", "candidate", "edge_fuzzy", fuzzy, "x")["error"] == "approved_graph_immutable"

    app.llm_client = FakeLLM()
    execution = app.generate_execution_prompt(task_id=task["task_id"])
    strategy = app.llm_generate_strategy_prompt(task_id=task["task_id"])
    assert "error" not in execution and "error" not in strategy, (execution, strategy)
    assert fuzzy in execution["prompt_content"] and fuzzy in strategy["prompt_content"]
    assert execution["route_table"] == strategy["route_table"]
    assert execution["route_table_sha256"] == strategy["route_table_sha256"]
    assert "未提供的信息表示未知，不得自动推导为否定" in execution["prompt_content"]

    compiled = app.compile_release(task_id=task["task_id"])
    assert "error" not in compiled, compiled
    compilation = app.get_compilation(compiled["compile_id"])
    manifest = compilation["manifest"]
    assert manifest["route_table_sha256"] == hashlib.sha256(compilation["route_table"].encode()).hexdigest()
    assert manifest["execution_prompt_sha256"] == hashlib.sha256(compilation["execution_prompt"].encode()).hexdigest()
    db = app._load_db()
    db.setdefault("gates", []).append({"gate_id": "G5", "task_id": task["task_id"], "decision": "approved"})
    app._save_db(db)
    release = app.create_release_package(compiled["compile_id"], "owner")
    assert "error" not in release, release
    assert release["execution_prompt"] == compilation["execution_prompt"]
    assert release["release_manifest"]["route_table_sha256"] == manifest["route_table_sha256"]

print(json.dumps({"status": "PASS", "contract": "edge-routes-v0.44"}, ensure_ascii=False))
