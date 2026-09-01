"""Isolated regression for v0.46 call-flow layout profiles."""
import json
import pathlib
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import app  # noqa: E402


class FakeLayoutLLM:
    def __init__(self):
        self.calls = 0

    def load_config(self):
        return {"model": "fake-layout"}

    def chat(self, messages, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        phases = ["pre_call", "connect_permission", "conversion", "closure_followup"]
        nodes = [
            {"node_id": node["id"], "phase_id": phases[index % len(phases)]}
            for index, node in enumerate(payload["nodes"])
        ]
        edges = [
            {"edge_id": edge["id"], "route_tendency": "receptive" if index % 2 else "resistant"}
            for index, edge in enumerate(payload["edges"])
        ]
        nodes.append({"node_id": "invented", "phase_id": "pre_call"})
        return {"content": json.dumps({"nodes": nodes, "edges": edges, "uncertainties": []})}


class FailingLayoutLLM:
    def chat(self, messages, **kwargs):
        return {"error": "provider_down"}


with tempfile.TemporaryDirectory() as tmp:
    app.DB_PATH = str(pathlib.Path(tmp) / "db.json")
    created = app.create_task("layout.txt", "hash-layout")
    task, source = created["task"], created["source"]
    db = app._load_db()
    baseline_graph = {
        "nodes": [{"id": "n1", "label": "外呼前读取画像"}, {"id": "n2", "label": "询问是否方便"}, {"id": "n3", "label": "邀请加微信"}],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "label": "接通"}, {"id": "e2", "source": "n2", "target": "n3", "label": "同意"}],
        "triggers": [], "stop_conditions": [],
    }
    baseline = app.create_graph_baseline(task["task_id"], source["source_id"], "layout", baseline_graph)
    db = app._load_db()
    db["sources"][source["source_id"]]["baseline_id"] = baseline["baseline_id"]
    next(item for item in db["tasks"] if item["task_id"] == task["task_id"])["baseline_id"] = baseline["baseline_id"]
    db["knowledge"] = {
        "graph_layout_test": {
            "object_id": "graph_layout_test", "type": "graph", "version": 1,
            "status": "candidate", "scope": "general", "content": "graph",
            "evidence_refs": [], "task_id": task["task_id"], "source_id": source["source_id"],
            "immutable": False, "linkage": {"baseline_id": baseline["baseline_id"], "node_ids": [], "edge_ids": [], "trigger_ids": []},
        }
    }
    app._save_db(db)
    graph_before = app.export_graph_document(task["task_id"], "graph_layout_test")
    missing = app.get_graph_layout(task["task_id"], "graph_layout_test")
    assert missing["status"] == "missing"

    fake = FakeLayoutLLM()
    app.llm_client = fake
    analyzed = app.analyze_graph_layout(task["task_id"], "graph_layout_test")
    assert analyzed["status"] == "ready" and fake.calls == 1
    assert analyzed["node_annotations"]["n1"]["phase_id"] == "pre_call"
    assert analyzed["edge_annotations"]["e1"]["route_tendency"] == "resistant"
    assert app.analyze_graph_layout(task["task_id"], "graph_layout_test")["deduplicated"] is True
    assert fake.calls == 1

    saved = app.save_graph_layout(
        task["task_id"], "graph_layout_test", analyzed["materialized_graph_hash"],
        [{"node_id": "n2", "phase_id": "needs_matching", "lane_override": "neutral"}],
        [{"edge_id": "e1", "route_tendency": "unknown"}], "reviewer",
    )
    assert saved["node_annotations"]["n2"] == {"phase_id": "needs_matching", "lane_override": "neutral", "source": "manual"}
    assert saved["edge_annotations"]["e1"]["route_tendency"] == "unknown"
    assert app.save_graph_layout(task["task_id"], "graph_layout_test", "old", [], [], "x")["error"] == "layout_stale"
    graph_after = app.export_graph_document(task["task_id"], "graph_layout_test")
    assert graph_after["content_hash"] == graph_before["content_hash"]
    assert graph_after["layout_sha256"] != graph_after["content_hash"]

    db = app._load_db()
    db["knowledge"]["node_new"] = {
        "object_id": "node_new", "type": "strategy_node", "version": 1,
        "status": "candidate", "scope": "general", "content": "新增收口",
        "evidence_refs": [], "task_id": task["task_id"], "source_id": source["source_id"],
        "immutable": False, "linkage": {"change_type": "add", "baseline_refs": []},
    }
    db["knowledge"]["graph_layout_test"]["linkage"]["node_ids"] = ["node_new"]
    db["knowledge"]["graph_layout_fail"] = {
        **db["knowledge"]["graph_layout_test"], "object_id": "graph_layout_fail",
        "linkage": {**db["knowledge"]["graph_layout_test"]["linkage"], "node_ids": []},
    }
    app._save_db(db)
    stale = app.get_graph_layout(task["task_id"], "graph_layout_test")
    assert stale["status"] == "stale" and stale["node_annotations"]["n2"]["source"] == "manual"
    refreshed = app.analyze_graph_layout(task["task_id"], "graph_layout_test")
    assert fake.calls == 2 and refreshed["node_annotations"]["n2"]["source"] == "manual"
    assert refreshed["node_annotations"]["node_new"]["phase_id"] != "unassigned"

    app.llm_client = FailingLayoutLLM()
    failed = app.analyze_graph_layout(task["task_id"], "graph_layout_fail")
    assert failed["error"] == "llm_call_failed"
    assert app.get_graph_layout(task["task_id"], "graph_layout_fail")["status"] == "failed"
    app.llm_client = fake

    db = app._load_db()
    db["knowledge"]["graph_layout_test"]["status"] = "approved"
    db["knowledge"]["graph_layout_test"]["immutable"] = True
    app._save_db(db)
    approved_layout = app.save_graph_layout(
        task["task_id"], "graph_layout_test", refreshed["materialized_graph_hash"],
        [{"node_id": "n2", "phase_id": "conversion", "lane_override": None}], [], "reviewer",
    )
    assert "error" not in approved_layout
    graph_after = app.export_graph_document(task["task_id"], "graph_layout_test")

    bundle = app.parse_graph_import_bundle(json.dumps(graph_after, ensure_ascii=False), graph_after["filename"])
    assert not bundle["warnings"] and bundle["layout_profile"]
    second = app.create_task("layout-consumer.txt", "hash-layout-consumer")
    imported = app.create_graph_baseline(
        second["task"]["task_id"], second["source"]["source_id"], "portable-layout",
        bundle["graph"], "portable_json", graph_after["filename"], bundle["layout_profile"],
    )
    assert imported.get("layout_id") and not imported.get("layout_warnings")
    assert app.get_graph_layout("wrong-task", "graph_layout_test")["error"] == "task_not_found"

print(json.dumps({"status": "PASS", "contract": "call-flow-layout-v0.46"}, ensure_ascii=False))
