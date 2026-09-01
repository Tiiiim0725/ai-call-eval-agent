"""Isolated regression for node script selection and corrected variants."""
import json
import pathlib
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import app  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.messages = []

    def chat(self, messages, **kwargs):
        self.messages.append(messages)
        return {"content": "ok", "model": "fake", "usage": {}}

    def get_config_snapshot(self):
        return {"model": "fake"}


with tempfile.TemporaryDirectory() as tmp:
    app.DB_PATH = str(pathlib.Path(tmp) / "db.json")
    created = app.create_task("script-variants.txt", "hash-script-variants")
    task, source = created["task"], created["source"]
    db = app._load_db()
    evidence = [
        {"evidence_id": "ev_1", "utterance_id": "utt_1", "source_id": source["source_id"],
         "task_id": task["task_id"], "speaker": "Kiki", "timestamp": "01:23",
         "content": "初始原话一", "status": "candidate", "evidence_kind": "utterance"},
        {"evidence_id": "ev_2", "utterance_id": "utt_2", "source_id": source["source_id"],
         "task_id": task["task_id"], "speaker": "Kiki", "timestamp": "02:34",
         "content": "初始原话二", "status": "candidate", "evidence_kind": "utterance"},
    ]
    db["evidence"] = evidence
    db["sources"][source["source_id"]]["target_expert"] = "Kiki"
    db["knowledge"] = {
        "group_1": {"object_id": "group_1", "type": "strategy_script_group", "version": 1,
                    "status": "candidate", "scope": "general", "content": "group",
                    "evidence_refs": ["ev_1"], "linkage": {}, "task_id": task["task_id"],
                    "source_id": source["source_id"], "immutable": False},
        "node_1": {"object_id": "node_1", "type": "strategy_node", "version": 1,
                   "status": "candidate", "scope": "general", "content": "测试节点",
                   "evidence_refs": ["ev_1", "ev_2"], "task_id": task["task_id"],
                   "source_id": source["source_id"], "immutable": False,
                   "linkage": {"group_id": "group_1", "script_evidence_refs": ["ev_1", "ev_2"]}},
        "graph_1": {"object_id": "graph_1", "type": "graph", "version": 1,
                    "status": "candidate", "scope": "general", "content": "graph",
                    "evidence_refs": ["ev_1"], "task_id": task["task_id"],
                    "source_id": source["source_id"], "immutable": False,
                    "linkage": {"group_id": "group_1", "node_ids": ["node_1"],
                                "edge_ids": [], "trigger_ids": []}},
    }
    app._save_db(db)

    workspace = app.get_node_script_workspace(task["task_id"], "graph_1", "node_1")
    assert [item["selected"] for item in workspace["items"]] == [True, True]
    assert all(item["selected_variant_id"] is None for item in workspace["items"])

    long_text = "人工听录屏校准后的完整原话" + ("细节" * 140)
    saved = app.create_script_variant(task["task_id"], "graph_1", "node_1", "ev_1", long_text, "editor")
    variant_id = saved["variant"]["object_id"]
    duplicate = app.create_script_variant(task["task_id"], "graph_1", "node_1", "ev_1", long_text, "editor")
    assert duplicate["deduplicated"] and duplicate["variant"]["object_id"] == variant_id

    second = app.create_script_variant(task["task_id"], "graph_1", "node_1", "ev_2", "待删除错误版本", "editor")
    deleted = app.delete_script_variant(task["task_id"], second["variant"]["object_id"], "editor")
    assert deleted["deleted"] and second["variant"]["object_id"] not in app._load_db()["knowledge"]

    selected = app.save_node_script_selections(
        task["task_id"], "graph_1", "node_1",
        [{"evidence_id": "ev_1", "variant_id": variant_id}], "editor",
    )
    assert selected["selected_count"] == 1
    db = app._load_db()
    materialized = app.materialize_incremental_graph(
        db, db["knowledge"]["graph_1"], [db["knowledge"]["node_1"]], None
    )
    examples = materialized["nodes"][0]["expert_utterances"]
    assert len(examples) == 1 and examples[0]["text"] == long_text
    assert examples[0]["evidence_text"] == "初始原话一"

    reviewed = app.review_graph_candidate(task["task_id"], "graph_1", "reviewer", "approved", "ok")
    assert "error" not in reviewed, reviewed
    db = app._load_db()
    assert db["knowledge"][variant_id]["status"] == "approved"
    approved_scripts = [item for item in db["knowledge"].values()
                        if item.get("type") == "script_fragment" and item.get("status") == "approved"]
    assert [item["object_id"] for item in approved_scripts] == [variant_id]
    assert app.delete_script_variant(task["task_id"], variant_id, "editor")["error"] == "approved_variant_immutable"

    fake = FakeLLM()
    app.llm_client = fake
    prompt = app.llm_generate_script_prompt(task_id=task["task_id"])
    assert "error" not in prompt, prompt
    assert long_text in fake.messages[-1][1]["content"], "人工校准长原话仍被固定 200 字截断"

print(json.dumps({"status": "PASS", "contract": "script-variants-v0.43"}, ensure_ascii=False))
