"""Isolated regression for exact-ID task cascade deletion."""
import json
import pathlib
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import app  # noqa: E402


with tempfile.TemporaryDirectory() as tmp:
    app.DB_PATH = str(pathlib.Path(tmp) / "db.json")
    doomed = app.create_task("delete-me.txt", "hash-delete")
    kept = app.create_task("keep-me.txt", "hash-keep")
    doomed_task, doomed_source = doomed["task"], doomed["source"]
    kept_task, kept_source = kept["task"], kept["source"]
    task_id, source_id = doomed_task["task_id"], doomed_source["source_id"]
    kept_task_id = kept_task["task_id"]

    db = app._load_db()
    next(item for item in db["tasks"] if item["task_id"] == kept_task_id)["rerun_of_task_id"] = task_id
    db["sources"][kept_source["source_id"]].update(
        rerun_of_task_id=task_id, origin_source_id=source_id
    )
    db["sessions"]["sess_delete"] = {"session_id": "sess_delete", "task_id": task_id, "source_id": source_id}
    db["utterances"].append({"utterance_id": "utt_delete", "task_id": task_id, "source_id": source_id})
    db["evidence"].append({"evidence_id": "ev_delete", "task_id": task_id, "source_id": source_id})
    db["knowledge"]["ko_delete"] = {"object_id": "ko_delete", "task_id": task_id, "source_id": source_id}
    db["gates"].append({"audit_id": "gate_delete", "task_id": task_id})
    db["analysis_runs"].append({"run_id": "run_delete", "task_id": task_id, "source_id": source_id})
    db["graph_baselines"].append({"baseline_id": "base_delete", "task_id": task_id, "source_id": source_id})
    db["script_document_candidates"].append({"candidate_id": "doc_delete", "task_id": task_id, "source_id": source_id})
    db["graph_layout_profiles"].append({"layout_id": "layout_delete", "task_id": task_id, "graph_id": "ko_delete"})
    db["compilations"].append({"compile_id": "cmp_delete", "manifest": {"task_id": task_id, "source_id": source_id}})
    db["releases"].append({"release_id": "rel_delete", "compile_id": "cmp_delete"})
    db["deliveries"].append({"delivery_id": "dlv_delete", "release_id": "rel_delete"})
    db["access_audit"].append({"audit_id": "aud_delete", "object_id": "ev_delete"})
    app._save_db(db)

    assert app.delete_task(task_id, "wrong-id")["error"] == "task_delete_confirmation_mismatch"
    result = app.delete_task(task_id, task_id, "tester")
    assert result["deleted"] is True
    db = app._load_db()
    assert [item["task_id"] for item in db["tasks"]] == [kept_task_id]
    assert source_id not in db["sources"] and "sess_delete" not in db["sessions"]
    assert not db["utterances"] and not db["evidence"] and "ko_delete" not in db["knowledge"]
    assert not db["gates"] and not db["analysis_runs"] and not db["graph_baselines"]
    assert not db["script_document_candidates"] and not db["graph_layout_profiles"]
    assert not db["compilations"] and not db["releases"] and not db["deliveries"]
    assert "rerun_of_task_id" not in db["tasks"][0] and db["tasks"][0]["rerun_origin_deleted"] is True
    assert "origin_source_id" not in db["sources"][kept_source["source_id"]]
    assert [item for item in db["access_audit"] if item.get("action") == "delete_task"]

print(json.dumps({"status": "PASS", "contract": "task-delete-v0.52"}, ensure_ascii=False))
