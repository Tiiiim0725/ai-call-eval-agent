"""Regression check: one immutable TXT source can start a fresh task run."""
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import app  # noqa: E402


def main():
    content = """2026年8月10日 下午 7:36|1分钟
文字记录:
胡旭 Looper 00:01
候选人说不方便时，我会先约下次时间。
"""
    with tempfile.TemporaryDirectory() as tmp:
        app.DB_PATH = str(Path(tmp) / "db.json")
        first = app.import_text_content("looper学习3.txt", content)
        old_id = first["task"]["task_id"]

        db = app._load_db()
        old_task = next(task for task in db["tasks"] if task["task_id"] == old_id)
        old_task.update(current_gate="G3", target_expert="looper", baseline_id="base_old")
        graph = {"nodes": [{"id": "n1", "label": "开场"}], "edges": [], "triggers": []}
        db["graph_baselines"].append({
            "baseline_id": "base_old",
            "task_id": old_id,
            "source_id": first["source"]["source_id"],
            "name": "流程图looper",
            "version": 1,
            "graph": graph,
            "content_hash": app._stable_fingerprint(graph),
            "immutable": True,
        })
        db["gates"].append({"task_id": old_id, "gate_id": "G1", "decision": "approved"})
        db["knowledge"]["ko_old"] = {"object_id": "ko_old", "task_id": old_id, "status": "candidate"}
        app._save_db(db)

        duplicate = app.import_text_content("改名也不是新任务.txt", content)
        assert duplicate == {
            "error": "duplicate",
            "task_id": old_id,
            "message": "该文件已导入，重复导入不覆盖旧快照",
        }

        rerun = app.rerun_task(old_id)
        new_task = rerun["task"]
        new_id = new_task["task_id"]
        assert new_id != old_id
        assert new_task["current_gate"] == "G0"
        assert new_task["target_expert"] is None
        assert "baseline_id" not in new_task
        assert new_task["rerun_of_task_id"] == old_id

        db = app._load_db()
        old_task = next(task for task in db["tasks"] if task["task_id"] == old_id)
        assert old_task["current_gate"] == "G3"
        assert not [gate for gate in db["gates"] if gate.get("task_id") == new_id]
        assert not [obj for obj in db["knowledge"].values() if obj.get("task_id") == new_id]
        new_evidence = [item for item in db["evidence"] if item.get("task_id") == new_id]
        assert new_evidence and all(item["status"] == "candidate" for item in new_evidence)
        assert all(item["evidence_kind"] == "utterance" for item in new_evidence)

        source = db["sources"][new_task["source_id"]]
        assert source["snapshot"] == content
        assert source["file_hash"] == first["source"]["file_hash"]
        assert source["origin_source_id"] == first["source"]["source_id"]
        new_baselines = [item for item in db["graph_baselines"] if item.get("task_id") == new_id]
        assert len(new_baselines) == 1
        assert new_baselines[0]["baseline_id"] != "base_old"
        assert new_baselines[0]["content_hash"] == app._stable_fingerprint(graph)
        assert new_baselines[0]["rerun_of_baseline_id"] == "base_old"

    print(json.dumps({"status": "PASS", "old_task": old_id, "new_task": new_id}, ensure_ascii=False))


if __name__ == "__main__":
    main()
