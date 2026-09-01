"""Read-only style regression for the H correction round.

The test uses an isolated JSON database and a deterministic fake LLM so it
does not consume the configured provider quota or mutate the demo database.
"""
import json
import os
import sys
import tempfile
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
import app  # noqa: E402


class FakeLLM:
    def chat(self, messages, max_tokens=4096):
        return {"content": "candidate prompt: " + messages[-1]["content"][:32],
                "model": "h-regression-fake", "usage": {}}

    def get_config_snapshot(self):
        return {"model": "h-regression-fake"}


def main():
    fd, db_path = tempfile.mkstemp(prefix="h_regression_", suffix=".json")
    os.close(fd)
    os.unlink(db_path)
    old_db, old_llm = app.DB_PATH, app.llm_client
    app.DB_PATH = db_path
    app.llm_client = FakeLLM()
    try:
        created = app.create_task("h-regression.txt", "h-regression-hash")
        task, source = created["task"], created["source"]
        db = app._load_db()
        db["evidence"].append({
            "evidence_id": "ev_h_regression",
            "utterance_id": "utt_h_regression",
            "source_id": source["source_id"],
            "task_id": task["task_id"],
            "speaker": "expert",
            "content": "方便后介绍岗位；不方便就停止。",
            "evidence_kind": "strategy",
            "status": "candidate",
        })
        app._save_db(db)
        assert app.llm_extract_strategy(source["source_id"])["error"] == "gate_required"
        assert app.gate_action("G1", task["task_id"], "operator", "approved",
                               "missing target")["error"] == "missing_target_expert"
        app.gate_action("G1", task["task_id"], "operator", "approved", "confirmed",
                        target_expert="expert")
        app.gate_action("G2", task["task_id"], "reviewer", "approved", "classified",
                        evidence_refs=["ev_h_regression"])

        n1 = app.create_knowledge_object(task["task_id"], source["source_id"], "strategy_node",
                                          "确认方便", ["ev_h_regression"], linkage={"group_id": "h-group"})
        n2 = app.create_knowledge_object(task["task_id"], source["source_id"], "strategy_node",
                                          "介绍岗位", ["ev_h_regression"], linkage={"group_id": "h-group"})
        edge = app.create_knowledge_object(task["task_id"], source["source_id"], "strategy_edge",
                                           "方便后继续", ["ev_h_regression"], linkage={
                                               "group_id": "h-group", "from_node_id": n1["object_id"],
                                               "to_node_id": n2["object_id"], "condition": "方便"})
        trigger = app.create_knowledge_object(task["task_id"], source["source_id"], "strategy_trigger",
                                              "不方便停止", ["ev_h_regression"], linkage={
                                                  "group_id": "h-group", "target_node_id": n1["object_id"],
                                                  "condition": "不方便"})
        graph = app.create_knowledge_object(task["task_id"], source["source_id"], "graph", "候选图",
                                            ["ev_h_regression"], linkage={
                                                "group_id": "h-group", "node_ids": [n1["object_id"], n2["object_id"]],
                                                "edge_ids": [edge["object_id"]], "trigger_ids": [trigger["object_id"]],
                                                "stop_conditions": ["不方便"]})
        for obj in (n1, n2, edge, trigger, graph):
            result = app.gate_action("G3", task["task_id"], "reviewer", "approved", "approved",
                                     target_object_id=obj["object_id"])
            assert "error" not in result, result

        prompt = app.llm_generate_strategy_prompt(source["source_id"], task["task_id"])
        assert "error" not in prompt, prompt
        assert set((graph["object_id"], edge["object_id"], trigger["object_id"])).issubset(prompt["input_objects"])
        compilation = app.compile_release(source["source_id"], task["task_id"])
        assert "error" not in compilation, compilation
        app.gate_action("G5", task["task_id"], "release-owner", "approved", "published")
        release = app.create_release_package(compilation["compile_id"])
        assert "error" not in release, release
        print(json.dumps({"status": "passed", "task_id": task["task_id"],
                          "compile_id": compilation["compile_id"], "release_id": release["release_id"]}))
    finally:
        app.DB_PATH, app.llm_client = old_db, old_llm
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    main()
