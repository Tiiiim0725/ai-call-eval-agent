"""PRD v0.38 regression checks against an isolated database and fake LLM."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
import app  # noqa: E402


class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.messages = []
        self.kwargs = []

    def chat(self, messages, **kwargs):
        self.messages.append(messages)
        self.kwargs.append(kwargs)
        return {"content": self.content, "model": "fake-v038", "usage": {}}

    def load_config(self):
        return {"base_url": "local", "model": "fake-v038", "temperature": 0, "max_tokens": 256}

    def get_config_snapshot(self):
        return self.load_config()


def seed(db_path):
    app.DB_PATH = db_path
    created = app.create_task("expert-check.txt", "hash-v038")
    task, source = created["task"], created["source"]
    db = app._load_db()
    db["utterances"] = [
        {"utterance_id": "utt_interviewer", "source_id": source["source_id"], "speaker": "访谈者", "content": "先问对方是否方便，再介绍岗位。"},
        {"utterance_id": "utt_expert", "source_id": source["source_id"], "speaker": "Kiki", "content": "如果不方便，我会先约下次时间。"},
        {"utterance_id": "utt_endorse", "source_id": source["source_id"], "speaker": "Kiki", "content": "对。"},
    ]
    db["evidence"] = [
        {"evidence_id": "ev_interviewer", "utterance_id": "utt_interviewer", "source_id": source["source_id"], "task_id": task["task_id"], "speaker": "访谈者", "content": "先问对方是否方便，再介绍岗位。", "evidence_kind": "utterance", "status": "candidate"},
        {"evidence_id": "ev_expert", "utterance_id": "utt_expert", "source_id": source["source_id"], "task_id": task["task_id"], "speaker": "Kiki", "content": "如果不方便，我会先约下次时间。", "evidence_kind": "utterance", "status": "candidate"},
        {"evidence_id": "ev_endorse", "utterance_id": "utt_endorse", "source_id": source["source_id"], "task_id": task["task_id"], "speaker": "Kiki", "content": "对。", "evidence_kind": "utterance", "status": "candidate"},
    ]
    app._save_db(db)
    assert "error" not in app.gate_action("G1", task["task_id"], "reviewer", "approved", "confirmed", target_expert="Kiki")
    return task, source


def check_evidence_attribution(tmp):
    task, source = seed(tmp)
    app.llm_client = FakeLLM(json.dumps([
        {"utterance_id": "utt_interviewer", "evidence_kind": "strategy", "reason": "策略"},
        {"utterance_id": "utt_expert", "evidence_kind": "strategy", "reason": "策略"},
    ], ensure_ascii=False))
    app.llm_extract_evidence(source["source_id"], 3)
    by_id = {item["evidence_id"]: item for item in app._load_db()["evidence"]}
    assert by_id["ev_interviewer"]["evidence_kind"] == "context", "非目标专家被写成 strategy"
    assert by_id["ev_expert"]["evidence_kind"] == "strategy"


def check_strategy_write_guard(tmp):
    task, source = seed(tmp)
    db = app._load_db()
    for item in db["evidence"]:
        item["evidence_kind"] = "strategy"
    app._save_db(db)
    app.llm_client = FakeLLM(json.dumps({
        "nodes": [{"node_name": "错误归属", "evidence_refs": ["utt_interviewer"], "is_fragment": True, "baseline_match": None}],
        "edges": [], "triggers": [], "candidate_states": []
    }, ensure_ascii=False))
    result = app.llm_extract_strategy(source["source_id"], 3)
    assert not result.get("nodes_created"), "非目标专家证据生成了策略节点"
    assert result.get("rejected_nodes"), "拒绝原因未返回"


def check_full_transcript_strategy(tmp):
    _, source = seed(tmp)
    fake = FakeLLM('{"nodes":[],"edges":[],"triggers":[],"candidate_states":[]}')
    app.llm_client = fake
    result = app.llm_extract_strategy(source["source_id"], 1, include_all=True)
    sent = json.dumps(fake.messages, ensure_ascii=False)
    assert "utt_interviewer" in sent and "utt_expert" in sent and "utt_endorse" in sent
    assert result.get("effective_max_utts") == 3, "完整访谈仍被单次条数限制截断"
    assert fake.kwargs[-1]["max_tokens"] >= 65536, "完整访谈没有获得高质量输出预算"
    assert fake.kwargs[-1]["thinking"] == {"type": "enabled"}, "完整访谈没有启用深度思考"


def check_incremental_contract_and_quality(tmp):
    task, source = seed(tmp)
    long_tail = "这是必须保留的访谈长发言结尾" + ("完整原话" * 80)
    baseline_script_tail = "这是必须保留的基线话术结尾" + ("基线原话" * 40)
    db = app._load_db()
    db["evidence"][1]["content"] = db["evidence"][1]["content"] + long_tail
    baseline_graph = {
        "nodes": [
            {
                "id": "base_opening",
                "label": "询问是否方便",
                "scripts": [{"text": "您好，现在方便接电话吗？" + baseline_script_tail}],
            },
            {"id": "base_followup", "label": "进入后续沟通", "scripts": []},
        ],
        "edges": [{"id": "base_edge", "source": "base_opening", "target": "base_followup", "label": "方便"}],
        "triggers": [],
    }
    baseline_hash = app._stable_fingerprint(baseline_graph)
    db["graph_baselines"] = [{
        "baseline_id": "base_quality",
        "task_id": task["task_id"],
        "version": 3,
        "content_hash": baseline_hash,
        "graph": baseline_graph,
    }]
    db["sources"][source["source_id"]]["baseline_id"] = "base_quality"
    db["tasks"][0]["baseline_id"] = "base_quality"
    app._save_db(db)

    fake = FakeLLM(json.dumps({
        "analysis_summary": "在既有开场节点上补充不方便时的后续动作",
        "nodes": [{
            "candidate_key": "opening_update",
            "change_type": "modify",
            "baseline_refs": ["base_opening"],
            "node_name": "询问是否方便并约定后续时间",
            "reason": "目标专家明确说明不方便时先约时间",
            "evidence_refs": ["utt_expert"],
            "context_refs": [],
            "script_evidence_refs": ["utt_expert"],
            "is_fragment": False,
        }],
        "edges": [{
            "candidate_key": "followup_edge_update",
            "change_type": "modify",
            "baseline_refs": ["base_edge"],
            "from_ref": "opening_update",
            "to_ref": "base_followup",
            "condition": "不方便时约定后续时间",
            "reason": "目标专家明确说明不方便时先约时间",
            "evidence_refs": ["utt_expert"],
            "context_refs": [],
        }],
        "triggers": [],
        "candidate_states": [],
        "uncertainties": [],
    }, ensure_ascii=False))
    app.llm_client = fake
    result = app.llm_extract_strategy(source["source_id"], 1, include_all=True)
    assert "error" not in result, result
    sent_user = fake.messages[-1][1]["content"]
    assert long_tail in sent_user, "完整访谈中的长发言仍被截断"
    assert baseline_script_tail in sent_user, "基线节点话术仍被截断"
    assert sent_user.index("base_opening") < sent_user.index("utt_expert"), "精确基线没有先于新访谈证据提供"
    sent_system = fake.messages[-1][0]["content"]
    for required in ("change_type", "baseline_refs", "script_evidence_refs"):
        assert required in sent_system, f"增量输出合约缺少 {required}"
    assert fake.kwargs[-1]["max_tokens"] >= 65536
    assert fake.kwargs[-1]["thinking"] == {"type": "enabled"}

    written = app._load_db()["knowledge"]
    graph = written[result["graph_id"]]
    assert graph["linkage"]["baseline_id"] == "base_quality"
    assert graph["linkage"]["baseline_content_hash"] == baseline_hash
    node = written[result["nodes_created"][0]["object_id"]]
    assert node["linkage"]["change_type"] == "modify"
    assert node["linkage"]["baseline_id"] == "base_quality"
    assert node["linkage"]["baseline_refs"] == ["base_opening"]
    assert node["linkage"]["script_evidence_refs"] == ["ev_expert"]
    assert not result["rejected_edges"], result["rejected_edges"]
    edge = written[result["edges"][0]["object_id"]]
    assert edge["linkage"]["baseline_id"] == "base_quality"
    assert edge["linkage"]["to_node_id"] == "base_followup", "未修改基线端点应被精确保留"
    reviewed = app.review_graph_candidate(
        task["task_id"], result["graph_id"], "reviewer", "approved", "整图与证据回链复核通过"
    )
    assert "error" not in reviewed, reviewed
    reviewed_db = app._load_db()
    assert reviewed_db["knowledge"][result["graph_id"]]["status"] == "approved"
    assert reviewed_db["knowledge"][result["nodes_created"][0]["object_id"]]["status"] == "approved"
    assert next(item for item in reviewed_db["evidence"] if item["evidence_id"] == "ev_expert")["evidence_kind"] == "script"
    graph_object = reviewed_db["knowledge"][result["graph_id"]]
    group_id = graph_object["linkage"]["group_id"]
    changes = [item for item in reviewed_db["knowledge"].values() if item.get("linkage", {}).get("group_id") == group_id]
    baseline = next(item for item in reviewed_db["graph_baselines"] if item["baseline_id"] == "base_quality")
    materialized = app.materialize_incremental_graph(reviewed_db, graph_object, changes, baseline)
    assert len(materialized["nodes"]) == 2, "编译物化丢失了未变化的基线节点"
    assert len(materialized["edges"]) == 1, "编译物化丢失了连接候选与基线节点的变更边"
    assert materialized["edges"][0]["origin"] == "candidate_change"
    changed_node = next(item for item in materialized["nodes"] if item["origin"] == "candidate_change")
    assert changed_node["expert_utterances"][0]["text"].endswith(long_tail), "编译物化没有携带不可变专家原话"
    assert any(item.get("type") == "script_fragment" and item.get("status") == "approved" for item in changes), "整图审核没有生成原话知识对象"


def check_invalid_baseline_reference_rejected(tmp):
    task, source = seed(tmp)
    db = app._load_db()
    baseline_graph = {"nodes": [{"id": "base_real", "label": "真实节点"}], "edges": [], "triggers": []}
    db["graph_baselines"] = [{
        "baseline_id": "base_refs",
        "task_id": task["task_id"],
        "version": 1,
        "content_hash": app._stable_fingerprint(baseline_graph),
        "graph": baseline_graph,
    }]
    db["sources"][source["source_id"]]["baseline_id"] = "base_refs"
    db["tasks"][0]["baseline_id"] = "base_refs"
    app._save_db(db)
    app.llm_client = FakeLLM(json.dumps({
        "nodes": [{
            "candidate_key": "bad",
            "change_type": "modify",
            "baseline_refs": ["made_up_node"],
            "node_name": "错误节点",
            "reason": "测试",
            "evidence_refs": ["utt_expert"],
            "context_refs": [],
            "script_evidence_refs": [],
            "is_fragment": True,
        }],
        "edges": [], "triggers": [], "candidate_states": [],
    }, ensure_ascii=False))
    result = app.llm_extract_strategy(source["source_id"], 3, include_all=True)
    assert not result.get("nodes_created"), "不存在的基线节点引用被写入候选图"
    assert result.get("rejected_nodes", [{}])[0].get("reason") == "invalid_baseline_ref"
    graph = app._load_db()["knowledge"][result["graph_id"]]
    assert graph["linkage"]["rejected_changes"], "服务端拒绝项没有进入候选 Graph 的可见状态"
    review = app.review_graph_candidate(task["task_id"], result["graph_id"], "reviewer", "approved", "test")
    assert review.get("error") == "candidate_change_rejections", "含拒绝项的候选 Graph 被错误批准"


def check_run_identity(tmp):
    task, source = seed(tmp)
    source_a = dict(source, target_expert="Kiki", target_expert_confirmation_version=1)
    source_b = dict(source, target_expert="Looper", target_expert_confirmation_version=2)
    fp_a = app._analysis_input_fingerprint(source_a, "strategy_extraction", run_options={"max_utts": 10, "prompt_version": "p1"})
    fp_b = app._analysis_input_fingerprint(source_b, "strategy_extraction", run_options={"max_utts": 10, "prompt_version": "p1"})
    fp_c = app._analysis_input_fingerprint(source_a, "strategy_extraction", run_options={"max_utts": 20, "prompt_version": "p1"})
    assert len({fp_a, fp_b, fp_c}) == 3, "专家或 max_utts 变化未使运行失效"


def check_exact_baseline_and_immutability(tmp):
    task, source = seed(tmp)
    db = app._load_db()
    for item in db["evidence"]:
        item["evidence_kind"] = "strategy"
    wrong_graph = {"nodes": [{"id": "n1", "label": "WRONG"}], "edges": [], "triggers": []}
    right_graph = {"nodes": [{"id": "n2", "label": "RIGHT"}], "edges": [], "triggers": []}
    db["graph_baselines"] = [
        {"baseline_id": "base_wrong", "task_id": task["task_id"], "version": 1, "content_hash": app._stable_fingerprint(wrong_graph), "graph": wrong_graph},
        {"baseline_id": "base_right", "task_id": task["task_id"], "version": 2, "content_hash": app._stable_fingerprint(right_graph), "graph": right_graph},
    ]
    db["sources"][source["source_id"]]["baseline_id"] = "base_right"
    db["tasks"][0]["baseline_id"] = "base_right"
    app._save_db(db)
    fake = FakeLLM('{"nodes":[],"edges":[],"triggers":[],"candidate_states":[]}')
    app.llm_client = fake
    app.llm_extract_strategy(source["source_id"], 3)
    sent = json.dumps(fake.messages, ensure_ascii=False)
    assert "RIGHT" in sent and "WRONG" not in sent, "分析未使用 G1 精确选择的基线"

    before = copy.deepcopy(app._load_db()["graph_baselines"][1])
    app.llm_client = FakeLLM('[{"node_id":"n2","text":"你好","script_type":"direct_script","reason":"匹配"}]')
    result = app.llm_map_script_documents(task["task_id"], "你好", "script.txt", baseline_id="base_right")
    after = app._load_db()["graph_baselines"][1]
    assert before == after, "话术导入原地修改了 immutable baseline"
    assert result.get("candidate_id"), "话术导入未生成独立候选"


def check_gate_guards(tmp):
    task, source = seed(tmp)
    raw = app.gate_action("G2", task["task_id"], "reviewer", "approved", "bad", evidence_refs=["ev_interviewer"])
    assert raw.get("error") == "invalid_evidence_kind", "原始 utterance 通过了 G2"

    db = app._load_db()
    db["evidence"][0]["evidence_kind"] = "strategy"
    db["evidence"][0]["status"] = "approved"
    db["evidence"][1]["evidence_kind"] = "strategy"
    app._save_db(db)
    assert "error" not in app.gate_action("G2", task["task_id"], "reviewer", "approved", "valid", evidence_refs=["ev_expert"])
    obj = app.create_knowledge_object(task["task_id"], source["source_id"], "strategy_node", "错误归属", ["ev_interviewer"], linkage={"group_id": "g"})
    assert obj.get("error") == "target_expert_evidence_required", "非目标专家知识在写入层未被阻断"
    db = app._load_db()
    db["knowledge"]["ko_legacy_bad"] = {"object_id": "ko_legacy_bad", "task_id": task["task_id"], "source_id": source["source_id"], "type": "strategy_node", "content": "历史错误归属", "evidence_refs": ["ev_interviewer"], "linkage": {"group_id": "g"}, "status": "candidate"}
    app._save_db(db)
    blocked = app.gate_action("G3", task["task_id"], "reviewer", "approved", "bad", target_object_id="ko_legacy_bad")
    assert blocked.get("error") == "target_expert_evidence_required", "非目标专家知识通过了 G3"


def run_check(name, fn):
    fd, path = tempfile.mkstemp(prefix="v038_", suffix=".json")
    os.close(fd)
    os.unlink(path)
    try:
        fn(path)
        return name, "PASS"
    except Exception as exc:
        return name, f"FAIL: {type(exc).__name__}: {exc}"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def main():
    old_db, old_llm = app.DB_PATH, app.llm_client
    try:
        checks = [
            ("evidence_attribution", check_evidence_attribution),
            ("strategy_write_guard", check_strategy_write_guard),
            ("full_transcript_strategy", check_full_transcript_strategy),
            ("incremental_contract_and_quality", check_incremental_contract_and_quality),
            ("invalid_baseline_reference_rejected", check_invalid_baseline_reference_rejected),
            ("run_identity", check_run_identity),
            ("exact_baseline_and_immutability", check_exact_baseline_and_immutability),
            ("gate_guards", check_gate_guards),
        ]
        results = [run_check(name, fn) for name, fn in checks]
        print(json.dumps(dict(results), ensure_ascii=False, indent=2))
        return 1 if any(value.startswith("FAIL") for _, value in results) else 0
    finally:
        app.DB_PATH, app.llm_client = old_db, old_llm


if __name__ == "__main__":
    raise SystemExit(main())
