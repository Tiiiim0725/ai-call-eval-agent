"""Isolated regression for editable imported baselines and portable Graph JSON."""
import json
import pathlib
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import app  # noqa: E402


with tempfile.TemporaryDirectory() as tmp:
    app.DB_PATH = str(pathlib.Path(tmp) / "db.json")
    created = app.create_task("portable.txt", "hash-portable")
    task, source = created["task"], created["source"]
    db = app._load_db()
    db["sources"][source["source_id"]]["target_expert"] = "Looper"
    app._save_db(db)

    imported = {
        "nodes": [
            {
                "id": "n1", "label": "旧节点", "kind": "action",
                "evidence_refs": ["portable_ev"], "context_refs": ["portable_ctx"],
                "scripts": [{"text": "旧话术", "timestamp": "01:23"}],
                "metadata": {"owner": "public"},
                "position": {"x": 10, "y": 20, "width": 180, "height": 70},
            },
            {"id": "n2", "label": "稀疏节点"},
        ],
        "edges": [{
            "id": "e1", "source": "n1", "target": "n2", "label": "旧条件",
            "evidence_refs": ["portable_edge_ev"], "context_refs": ["portable_edge_ctx"],
            "condition_review_status": "confirmed", "metadata": {"source": "public"},
        }],
        "triggers": [], "stop_conditions": [],
    }
    baseline = app.create_graph_baseline(
        task["task_id"], source["source_id"], "public", imported, "portable_json", "public.json"
    )
    assert "error" not in baseline, baseline
    sparse = next(node for node in baseline["graph"]["nodes"] if node["id"] == "n2")
    assert sparse["evidence_refs"] == sparse["context_refs"] == sparse["scripts"] == []
    assert sparse["metadata"] == {}
    original_hash = baseline["content_hash"]

    db = app._load_db()
    db["knowledge"] = {
        "group_public": {
            "object_id": "group_public", "type": "strategy_script_group", "version": 1,
            "status": "candidate", "scope": "general", "content": "group",
            "evidence_refs": [], "linkage": {}, "task_id": task["task_id"],
            "source_id": source["source_id"], "immutable": False,
        },
        "graph_public": {
            "object_id": "graph_public", "type": "graph", "version": 1,
            "status": "candidate", "scope": "general", "content": "graph",
            "evidence_refs": [], "task_id": task["task_id"],
            "source_id": source["source_id"], "immutable": False,
            "linkage": {
                "group_id": "group_public", "baseline_id": baseline["baseline_id"],
                "baseline_version": baseline["version"],
                "baseline_content_hash": baseline["content_hash"],
                "node_ids": [], "edge_ids": [], "trigger_ids": [],
            },
        },
    }
    app._save_db(db)

    node_saved = app.save_node_content(
        task["task_id"], "graph_public", "baseline", "n1", "人工修改节点", "reviewer"
    )
    assert "error" not in node_saved, node_saved
    assert node_saved["node_origin"] == "candidate"
    candidate_node_id = node_saved["node_id"]
    node_saved_again = app.save_node_content(
        task["task_id"], "graph_public", "candidate", candidate_node_id, "人工修改节点 v2", "reviewer"
    )
    assert node_saved_again["current_content"] == "人工修改节点 v2"
    assert node_saved_again["original_content"] == "旧节点"

    edge_saved = app.save_edge_condition(
        task["task_id"], "graph_public", "baseline", "e1", "人工修改条件", "reviewer"
    )
    assert "error" not in edge_saved, edge_saved
    db = app._load_db()
    current_baseline = next(item for item in db["graph_baselines"] if item["baseline_id"] == baseline["baseline_id"])
    assert current_baseline["content_hash"] == original_hash
    assert app._stable_fingerprint(current_baseline["graph"]) == original_hash

    graph = db["knowledge"]["graph_public"]
    materialized = app.materialize_incremental_graph(
        db, graph, app._graph_change_objects(db, graph), current_baseline
    )
    changed_node = next(node for node in materialized["nodes"] if node["label"] == "人工修改节点 v2")
    assert changed_node["evidence_refs"] == ["portable_ev"]
    assert changed_node["context_refs"] == ["portable_ctx"]
    assert changed_node["scripts"][0]["text"] == "旧话术"
    assert changed_node["metadata"] == {"owner": "public"}
    assert changed_node["position"]["x"] == 10
    changed_edge = next(edge for edge in materialized["edges"] if edge["condition"] == "人工修改条件")
    assert changed_edge["evidence_refs"] == ["portable_edge_ev"]
    assert changed_edge["context_refs"] == ["portable_edge_ctx"]
    assert changed_edge["metadata"] == {"source": "public"}

    exported = app.export_graph_document(task["task_id"], "graph_public")
    assert "error" not in exported, exported
    assert exported["content_hash"] == app._stable_fingerprint(exported["graph"])
    assert app.export_graph_document(task["task_id"], "graph_public")["content_hash"] == exported["content_hash"]
    assert next(node for node in exported["graph"]["nodes"] if node["label"] == "人工修改节点 v2")["scripts"]
    reparsed = app.parse_graph_import(json.dumps(exported, ensure_ascii=False), exported["filename"])
    assert "error" not in reparsed, reparsed
    assert app._stable_fingerprint(reparsed) == exported["content_hash"]

    second = app.create_task("consumer.txt", "hash-consumer")
    imported_again = app.create_graph_baseline(
        second["task"]["task_id"], second["source"]["source_id"], "reused", reparsed,
        "portable_json", exported["filename"],
    )
    assert imported_again["content_hash"] == exported["content_hash"]
    assert app.create_graph_baseline(
        second["task"]["task_id"], source["source_id"], "cross", reparsed
    )["error"] == "cross_task_source"
    assert app._normalize_graph_document({
        "nodes": [{"id": "x", "label": "x", "scripts": "not-a-list"}],
        "edges": [], "triggers": [],
    })["error"] == "invalid_graph_node"

    db = app._load_db()
    db["knowledge"]["graph_public"]["status"] = "approved"
    db["knowledge"]["graph_public"]["immutable"] = True
    app._save_db(db)
    assert app.save_node_content(
        task["task_id"], "graph_public", "candidate", candidate_node_id, "不能改", "reviewer"
    )["error"] == "approved_graph_immutable"

print(json.dumps({"status": "PASS", "contract": "portable-baseline-v0.45"}, ensure_ascii=False))
