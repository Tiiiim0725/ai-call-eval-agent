"""
AI 电话评价 Agent｜MVP 后端服务
B 阶段：来源、解析与证据
端口：8898

输入文件格式为飞书会议记录导出：
  第一行：日期 + 时长
  关键词: ...
  文字记录:
  说话人 MM:SS
  发言内容（可跨多行）
"""
import json
import hashlib
import os
import re
import sys
import time
import traceback
import uuid
import html as html_lib
from xml.etree import ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import llm_client
except ImportError:
    llm_client = None

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "db.json")
INPUT_DIR = os.path.join(APP_ROOT, "..", "input-docs")
EVIDENCE_PROMPT_VERSION = "v0.38-evidence-1"
STRATEGY_PROMPT_VERSION = "v0.39-incremental-graph-1"
SCRIPT_PROMPT_VERSION = "v0.38-script-1"
ANALYSIS_SCHEMA_VERSION = "v0.39-2"
VALID_EVIDENCE_KINDS = {"strategy", "script", "context", "meta"}
GRAPH_CHANGE_TYPES = {"add", "modify", "deprecate", "split", "merge", "keep"}
GRAPH_LAYOUT_PROMPT_VERSION = "v0.46-call-flow-layout-1"
GRAPH_LAYOUT_PHASES = [
    {"phase_id": "pre_call", "label": "外呼前准备", "order": 1},
    {"phase_id": "connect_permission", "label": "接通与身份许可", "order": 2},
    {"phase_id": "availability_routing", "label": "可用性与初始分流", "order": 3},
    {"phase_id": "intent_objection", "label": "意愿识别与异议处理", "order": 4},
    {"phase_id": "needs_matching", "label": "需求澄清与机会匹配", "order": 5},
    {"phase_id": "conversion", "label": "转化动作", "order": 6},
    {"phase_id": "closure_followup", "label": "收口与后续", "order": 7},
]
GRAPH_LAYOUT_PHASE_IDS = {item["phase_id"] for item in GRAPH_LAYOUT_PHASES}
GRAPH_LAYOUT_TENDENCIES = {"resistant", "neutral", "receptive", "unknown"}
GRAPH_LAYOUT_LANES = {"resistant", "neutral", "receptive"}


def _load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "r", encoding="utf-8") as f:
            db = json.load(f)
            db.setdefault("analysis_runs", [])
            db.setdefault("graph_baselines", [])
            db.setdefault("graph_match_confirmations", [])
            db.setdefault("script_document_candidates", [])
            db.setdefault("graph_layout_profiles", [])
            return db
    return {"tasks": [], "sources": {}, "sessions": {}, "utterances": [], "evidence": [], "knowledge": {}, "changes": [], "gates": [], "access_audit": [], "compilations": [], "releases": [], "deliveries": [], "analysis_runs": [], "graph_baselines": [], "graph_match_confirmations": [], "script_document_candidates": [], "graph_layout_profiles": []}


def _save_db(db):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _stable_fingerprint(value) -> str:
    """Return a deterministic fingerprint for a run or candidate structure."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_candidate_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _llm_config_fingerprint():
    if not llm_client:
        return "unavailable"
    try:
        config = llm_client.load_config()
        safe = {key: config.get(key) for key in ("base_url", "model", "temperature", "max_tokens")}
        return _stable_fingerprint(safe)
    except Exception:
        return "unknown"


def _analysis_input_fingerprint(source, operation, evidence=None, knowledge=None,
                                run_options=None, baseline=None):
    """Fingerprint only the inputs that should make a new LLM run meaningful."""
    evidence_state = []
    for item in evidence or []:
        evidence_state.append({
            "evidence_id": item.get("evidence_id"),
            "utterance_id": item.get("utterance_id"),
            "evidence_kind": item.get("evidence_kind"),
            "status": item.get("status"),
            "conflict_set": item.get("conflict_set"),
        })
    knowledge_state = []
    for item in knowledge or []:
        knowledge_state.append({
            "object_id": item.get("object_id"),
            "type": item.get("type"),
            "content": item.get("content"),
            "evidence_refs": sorted(set(item.get("evidence_refs") or [])),
            "linkage": item.get("linkage") or {},
            "status": item.get("status"),
        })
    return _stable_fingerprint({
        "task_id": source.get("task_id"),
        "source_hash": source.get("file_hash"),
        "target_expert": source.get("target_expert"),
        "target_expert_confirmation_version": source.get("target_expert_confirmation_version", 0),
        "operation": operation,
        "evidence": evidence_state,
        "knowledge": knowledge_state,
        "baseline": ({
            "baseline_id": baseline.get("baseline_id"),
            "version": baseline.get("version"),
            "content_hash": baseline.get("content_hash"),
        } if baseline else None),
        "run_options": run_options or {},
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "llm_config": _llm_config_fingerprint(),
    })


def _find_completed_analysis_run(source_id, operation, input_fingerprint):
    db = _load_db()
    matches = [run for run in db.get("analysis_runs", [])
               if run.get("source_id") == source_id
               and run.get("operation") == operation
               and run.get("input_fingerprint") == input_fingerprint
               and run.get("status") == "completed"]
    return matches[-1] if matches else None


def _create_analysis_run(source, operation, input_fingerprint, run_metadata=None):
    db = _load_db()
    run = {
        "run_id": _new_id("run"),
        "task_id": source.get("task_id"),
        "source_id": source.get("source_id"),
        "operation": operation,
        "input_fingerprint": input_fingerprint,
        "target_expert": source.get("target_expert"),
        "target_expert_confirmation_version": source.get("target_expert_confirmation_version", 0),
        "llm_config_fingerprint": _llm_config_fingerprint(),
        "run_metadata": run_metadata or {},
        "status": "running",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "result": None,
    }
    db.setdefault("analysis_runs", []).append(run)
    _save_db(db)
    return run


def _finish_analysis_run(run_id, result, status="completed"):
    db = _load_db()
    for run in db.get("analysis_runs", []):
        if run.get("run_id") == run_id:
            run["status"] = status
            run["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            run["result"] = result
            break
    _save_db(db)


def _reuse_analysis_run(run):
    result = dict(run.get("result") or {})
    result["run_id"] = run.get("run_id")
    result["deduplicated"] = True
    result["message"] = "相同输入已完成过本项分析，已复用已有结果，未重复写入"
    return result


def _speaker_key(value):
    return re.sub(r"[\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _is_target_expert_evidence(source, evidence):
    return bool(_speaker_key(source.get("target_expert"))) and (
        _speaker_key(source.get("target_expert")) == _speaker_key(evidence.get("speaker"))
    )


def _is_terse_endorsement(content):
    return bool(re.fullmatch(r"\s*(对|对的|是|是的|嗯|嗯嗯|没错|可以)[。！!，,\.\s]*", str(content or "")))


def _selected_baseline(db, source, baseline_id=None):
    task = _task_for(db, source.get("task_id", ""))
    selected_id = baseline_id or source.get("baseline_id") or (task or {}).get("baseline_id")
    if not selected_id:
        return None, None
    baseline = next((item for item in db.get("graph_baselines", [])
                     if item.get("baseline_id") == selected_id
                     and item.get("task_id") == source.get("task_id")), None)
    if not baseline:
        return None, {"error": "baseline_not_found", "baseline_id": selected_id}
    actual_hash = _stable_fingerprint(baseline.get("graph", {}))
    if baseline.get("content_hash") != actual_hash:
        return None, {"error": "baseline_hash_mismatch", "baseline_id": selected_id}
    return baseline, None


def _effective_max_utts(requested):
    requested = max(1, int(requested or 1))
    limit = 10
    if llm_client:
        try:
            limit = max(1, int(llm_client.load_config().get("max_utterances_per_call", 10)))
        except Exception:
            pass
    return min(requested, limit)


def _expert_support_error(db, source, evidence_refs, context_refs=None):
    refs = normalize_evidence_refs(db, evidence_refs)
    contexts = normalize_evidence_refs(db, context_refs or [])
    if not refs:
        return {"error": "missing_evidence"}
    unknown = validate_evidence_refs(db, refs + contexts)
    if unknown:
        return {"error": "unknown_evidence_ref", "evidence_refs": unknown}
    support = [_evidence_for(db, ref) for ref in refs]
    if any(not _is_target_expert_evidence(source, item or {}) for item in support):
        return {"error": "target_expert_evidence_required", "evidence_refs": refs}
    if support and all(_is_terse_endorsement(item.get("content")) for item in support) and not contexts:
        return {"error": "endorsement_context_required", "evidence_refs": refs}
    return None


def _valid_baseline_condition_correction(db, task_id, linkage):
    """Allow label-only fixes to an exact immutable baseline edge without inventing endpoints."""
    linkage = linkage or {}
    refs = [str(ref) for ref in (linkage.get("baseline_refs") or [])]
    if (linkage.get("baseline_condition_correction") is not True
            or linkage.get("change_type") != "modify" or len(refs) != 1
            or not str(linkage.get("condition") or "").strip()):
        return False
    baseline = next((item for item in db.get("graph_baselines", [])
                     if item.get("task_id") == task_id
                     and item.get("baseline_id") == linkage.get("baseline_id")), None)
    if (not baseline or baseline.get("content_hash") != _stable_fingerprint(baseline.get("graph") or {})
            or linkage.get("baseline_version") != baseline.get("version")
            or linkage.get("baseline_content_hash") != baseline.get("content_hash")):
        return False
    raw = _baseline_indexes(baseline)["edges"]["by_id"].get(refs[0])
    if not raw:
        return False
    raw_from = str(raw.get("source") or raw.get("from_node_id") or raw.get("from") or "")
    raw_to = str(raw.get("target") or raw.get("to_node_id") or raw.get("to") or "")
    return (str(linkage.get("from_node_id") or "") == raw_from
            and str(linkage.get("to_node_id") or "") == raw_to)


def _valid_baseline_node_correction(db, task_id, linkage):
    """Allow text-only edits to one exact immutable baseline node."""
    linkage = linkage or {}
    refs = [str(ref) for ref in (linkage.get("baseline_refs") or [])]
    if (linkage.get("baseline_node_correction") is not True
            or linkage.get("change_type") != "modify" or len(refs) != 1
            or not str(linkage.get("node_content") or "").strip()):
        return False
    baseline = next((item for item in db.get("graph_baselines", [])
                     if item.get("task_id") == task_id
                     and item.get("baseline_id") == linkage.get("baseline_id")), None)
    return bool(
        baseline
        and baseline.get("content_hash") == _stable_fingerprint(baseline.get("graph") or {})
        and linkage.get("baseline_version") == baseline.get("version")
        and linkage.get("baseline_content_hash") == baseline.get("content_hash")
        and _baseline_indexes(baseline)["nodes"]["by_id"].get(refs[0])
    )


def _context_window_evidence(all_evidence, eligible, limit):
    """Keep eligible expert evidence plus immediate neighbours as separate context."""
    eligible_ids = {item.get("evidence_id") for item in eligible}
    selected = []
    seen = set()
    for index, item in enumerate(all_evidence):
        if item.get("evidence_id") not in eligible_ids:
            continue
        for candidate in all_evidence[max(0, index - 1):index + 2]:
            evidence_id = candidate.get("evidence_id")
            if evidence_id not in seen:
                selected.append(candidate)
                seen.add(evidence_id)
        if len(selected) >= limit:
            break
    return selected[:limit]


def _baseline_indexes(baseline):
    """Index immutable baseline entities without changing the imported document."""
    graph = (baseline or {}).get("graph") or {}
    indexes = {}
    for entity_type in ("nodes", "edges", "triggers"):
        items = graph.get(entity_type) or []
        by_id = {str(item.get("id")): item for item in items if item.get("id") is not None}
        by_label = {}
        for item in items:
            label = _normalize_candidate_text(item.get("label") or item.get("condition"))
            if label and label not in by_label:
                by_label[label] = str(item.get("id"))
        indexes[entity_type] = {"by_id": by_id, "by_label": by_label}
    return indexes


def _normalize_baseline_refs(raw_refs, legacy_match, entity_index):
    """Resolve model refs to exact baseline IDs; labels remain a legacy fallback."""
    refs = raw_refs if isinstance(raw_refs, list) else ([] if raw_refs in (None, "") else [raw_refs])
    if not refs and legacy_match not in (None, ""):
        refs = [legacy_match]
    normalized = []
    invalid = []
    for raw in refs:
        value = str(raw or "").strip()
        if not value:
            continue
        if value in entity_index["by_id"]:
            resolved = value
        else:
            resolved = entity_index["by_label"].get(_normalize_candidate_text(value))
        if not resolved:
            invalid.append(value)
        elif resolved not in normalized:
            normalized.append(resolved)
    return normalized, invalid


def _normalize_change_type(raw_type, baseline_refs, has_baseline):
    value = str(raw_type or "").strip().lower()
    if value not in GRAPH_CHANGE_TYPES:
        value = "modify" if baseline_refs else "add"
    if not has_baseline:
        return "add" if value in ("add", "keep") else value
    return value


def _change_contract_error(change_type, baseline_refs, has_baseline):
    if not has_baseline:
        return None if change_type == "add" and not baseline_refs else "baseline_required"
    if change_type == "add" and baseline_refs:
        return "add_must_not_reference_baseline"
    if change_type in {"modify", "deprecate", "split", "keep"} and not baseline_refs:
        return "baseline_ref_required"
    if change_type == "merge" and len(baseline_refs) < 2:
        return "merge_requires_multiple_baseline_refs"
    return None


def _evidence_prompt_item(item, source):
    return {
        "role": "SUPPORT" if _is_target_expert_evidence(source, item) else "CONTEXT",
        "evidence_id": item.get("evidence_id"),
        "utterance_id": item.get("utterance_id"),
        "speaker": item.get("speaker"),
        "timestamp": item.get("timestamp"),
        "content": item.get("content", ""),
    }


# ── B-01: 数据结构 ─────────────────────────────────
def create_task(filename: str, file_hash: str, meta: dict = None) -> dict:
    db = _load_db()
    task_id = _new_id("task")
    source_id = _new_id("src")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    task = {
        "task_id": task_id,
        "status": "imported",
        "source_id": source_id,
        "filename": filename,
        "file_hash": file_hash,
        "target_expert": None,
        "current_gate": "G0",
        "created_at": now,
        "updated_at": now,
    }

    source = {
        "source_id": source_id,
        "task_id": task_id,
        "filename": filename,
        "file_hash": file_hash,
        "snapshot": None,
        "meta": meta or {},
        "imported_at": now,
        "immutable": True,
    }

    db["tasks"].append(task)
    db["sources"][source_id] = source
    _save_db(db)
    return {"task": task, "source": source}


def get_task(task_id: str):
    db = _load_db()
    for t in db["tasks"]:
        if t["task_id"] == task_id:
            return t
    return None


# ── B-02: 飞书格式 TXT 导入与解析 ────────────────────
def import_txt(filepath: str, filename: str = None) -> dict:
    if filename is None:
        filename = os.path.basename(filepath)

    with open(filepath, "rb") as f:
        raw_bytes = f.read()
    file_hash = _sha256_bytes(raw_bytes)

    db = _load_db()
    for t in db["tasks"]:
        if t.get("file_hash") == file_hash:
            return {"error": "duplicate", "task_id": t["task_id"],
                    "message": "该文件已导入，重复导入不覆盖旧快照"}

    snapshot = raw_bytes.decode("utf-8", errors="replace")

    # 提取元信息
    meta = parse_meta(snapshot)

    result = create_task(filename, file_hash, meta)

    # 保存原始快照
    db = _load_db()
    source_id = result["source"]["source_id"]
    db["sources"][source_id]["snapshot"] = snapshot
    _save_db(db)

    # 立即解析
    parse_result = parse_source(source_id)
    result["parse"] = parse_result
    return result


def import_text_content(filename: str, content: str) -> dict:
    """Import a user-selected UTF-8 TXT without requiring server-side file copy."""
    if not filename:
        return {"error": "missing filename"}
    if not isinstance(content, str):
        return {"error": "invalid_content"}
    raw_bytes = content.encode("utf-8")
    file_hash = _sha256_bytes(raw_bytes)
    db = _load_db()
    for task in db.get("tasks", []):
        if task.get("file_hash") == file_hash:
            return {"error": "duplicate", "task_id": task["task_id"],
                    "message": "该文件已导入，重复导入不覆盖旧快照"}
    result = create_task(filename, file_hash, parse_meta(content))
    db = _load_db()
    source_id = result["source"]["source_id"]
    db["sources"][source_id]["snapshot"] = content
    _save_db(db)
    result["parse"] = parse_source(source_id)
    return result


def rerun_task(task_id: str) -> dict:
    """Start a clean task from an existing immutable source snapshot."""
    db = _load_db()
    old_task = next((task for task in db.get("tasks", []) if task.get("task_id") == task_id), None)
    if not old_task:
        return {"error": "task_not_found"}
    old_source = db.get("sources", {}).get(old_task.get("source_id"))
    if not old_source:
        return {"error": "source_not_found"}
    if not old_source.get("snapshot"):
        return {"error": "no_snapshot"}

    result = create_task(old_source.get("filename") or old_task.get("filename", ""),
                         old_source.get("file_hash", ""), dict(old_source.get("meta") or {}))
    db = _load_db()
    new_task = next(task for task in db["tasks"] if task["task_id"] == result["task"]["task_id"])
    new_source = db["sources"][new_task["source_id"]]
    new_task["rerun_of_task_id"] = task_id
    new_source.update({
        "snapshot": old_source["snapshot"],
        "origin_source_id": old_source.get("origin_source_id") or old_source["source_id"],
        "rerun_of_task_id": task_id,
    })
    copied_baselines = []
    for baseline in list(db.get("graph_baselines", [])):
        if baseline.get("task_id") != task_id:
            continue
        copied = dict(baseline)
        copied.update({
            "baseline_id": _new_id("base"),
            "task_id": new_task["task_id"],
            "source_id": new_source["source_id"],
            "rerun_of_baseline_id": baseline.get("baseline_id"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        db["graph_baselines"].append(copied)
        copied_baselines.append(copied)
    _save_db(db)
    result.update(task=new_task, source=new_source, baselines=copied_baselines,
                  parse=parse_source(new_source["source_id"]))
    return result


def parse_meta(text: str) -> dict:
    """从飞书记录中提取元信息：日期、时长、关键词"""
    meta = {}
    lines = text.strip().split("\n")

    # 第一行：日期 + 时长
    if lines:
        first = lines[0].strip()
        # 格式如 "2026年7月28日 下午 4:35|35分钟 36秒"
        meta["header"] = first
        parts = first.split("|")
        if len(parts) >= 1:
            meta["date"] = parts[0].strip()
        if len(parts) >= 2:
            meta["duration"] = parts[1].strip()

    # 关键词行（飞书格式："关键词:" 单独一行，内容在下一行）
    for i, line in enumerate(lines):
        line_s = line.strip()
        if line_s.startswith("关键词"):
            # 内容可能在同行或下一行
            kw_str = line_s.replace("关键词:", "").replace("关键词：", "").replace("关键词", "").strip()
            if not kw_str and i + 1 < len(lines):
                kw_str = lines[i + 1].strip()
            meta["keywords"] = [k.strip() for k in re.split(r"[、，,]", kw_str) if k.strip()]
            break

    return meta


def parse_source(source_id: str) -> dict:
    """解析来源 TXT：提取会话、发言、证据"""
    db = _load_db()
    source = db["sources"].get(source_id)
    if not source:
        return {"error": "source_not_found"}

    snapshot = source.get("snapshot", "")
    if not snapshot:
        return {"error": "no_snapshot"}

    task_id = source["task_id"]

    # 重复解析不覆盖
    existing = [s for s in db["sessions"].values() if s.get("source_id") == source_id]
    if existing:
        return {"error": "already_parsed", "sessions": len(existing),
                "message": "已解析，重复解析保留已有版本"}

    # ── 飞书格式解析 ──
    # 格式：
    #   说话人 MM:SS
    #   发言内容（可能跨多行，直到下一个 "说话人 MM:SS" 或文件结束）
    lines = snapshot.strip().split("\n")

    # 找到 "文字记录:" 的位置
    record_start = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("文字记录"):
            record_start = i + 1
            break

    # 解析发言
    session_id = _new_id("sess")
    session = {
        "session_id": session_id,
        "source_id": source_id,
        "task_id": task_id,
        "label": source.get("meta", {}).get("date", "default"),
        "utterance_count": 0,
    }

    utterances = []
    evidence = []

    # 说话人行正则：匹配 "名字 HH:MM" 或 "名字 MM:SS"
    # 名字可以包含中文、字母、空格
    speaker_re = re.compile(r"^(.+?)\s*([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)\s*$")

    i = record_start
    while i < len(lines):
        line = lines[i].strip()
        m = speaker_re.match(line)
        if m:
            speaker = m.group(1).strip()
            timestamp = m.group(2).strip()

            # 收集发言内容直到下一个说话人行
            content_lines = []
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if next_line and speaker_re.match(next_line):
                    break
                if next_line:
                    content_lines.append(next_line)
                i += 1

            content = "".join(content_lines)

            # 计算字节偏移（在原文中找该发言的起始位置）
            # 先在原文中找到说话人行，再找到内容
            search_from = 0
            # 用累积偏移找到该行在原文中的位置
            line_offset = "\n".join(lines[:i - len(content_lines) - 1]).__len__() if i - len(content_lines) - 1 > 0 else 0
            if i - len(content_lines) - 1 > 0:
                line_offset = len("\n".join(lines[:i - len(content_lines) - 1])) + 1  # +1 for the newline
            else:
                line_offset = 0

            # 更精确：找到说话人+时间戳这行在原文中的位置
            full_speaker_line = f"{speaker} {timestamp}"
            char_start = snapshot.find(full_speaker_line, 0)
            if char_start == -1:
                # 尝试不带空格
                char_start = snapshot.find(f"{speaker}{timestamp}", 0)
            if char_start == -1:
                # fallback
                char_start = 0

            # 内容的 char offset = 说话人行的位置 + 该行长度 + 换行
            content_char_offset = char_start + len(full_speaker_line)
            # 跳过换行
            if content_char_offset < len(snapshot) and snapshot[content_char_offset] == "\n":
                content_char_offset += 1

            byte_offset = len(snapshot[:content_char_offset].encode("utf-8", errors="replace"))

            uid = _new_id("utt")
            utt = {
                "utterance_id": uid,
                "session_id": session_id,
                "source_id": source_id,
                "task_id": task_id,
                "line_num": i - len(content_lines),
                "speaker": speaker,
                "timestamp": timestamp,
                "content": content,
                "char_offset": content_char_offset,
                "byte_offset": byte_offset,
            }
            utterances.append(utt)
            db["utterances"].append(utt)

            # B-03: 每条发言生成候选证据
            eid = _new_id("ev")
            ev = {
                "evidence_id": eid,
                "source_id": source_id,
                "task_id": task_id,
                "utterance_id": uid,
                "session_id": session_id,
                "speaker": speaker,
                "timestamp": timestamp,
                "content": content,
                "char_offset": content_char_offset,
                "byte_offset": byte_offset,
                "span": [content_char_offset, content_char_offset + len(content)],
                "evidence_kind": "utterance",
                "source_hash": source["file_hash"],
                "context_refs": [],
                "conflict_set": None,
                "status": "candidate",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            evidence.append(ev)
            db["evidence"].append(ev)
        else:
            i += 1

    session["utterance_count"] = len(utterances)
    db["sessions"][session_id] = session
    response = {
        "source_id": source_id,
        "sessions": 1,
        "utterances": len(utterances),
        "evidence": len(evidence),
    }
    _save_db(db)
    return response



def approved_gate(db, task_id, gate_id, target_object_id=None):
    """Return whether a task has an immutable approved Gate record."""
    for gate in db.get("gates", []):
        if gate.get("task_id") != task_id or gate.get("gate_id") != gate_id:
            continue
        if gate.get("decision") != "approved":
            continue
        if target_object_id and gate.get("target_object_id") != target_object_id:
            continue
        return True
    return False


def _task_for(db, task_id):
    return next((task for task in db.get("tasks", [])
                 if task.get("task_id") == task_id), None)


def _evidence_for(db, evidence_id):
    return next((item for item in db.get("evidence", [])
                 if item.get("evidence_id") == evidence_id), None)


def _approved_object(db, object_id):
    obj = db.get("knowledge", {}).get(object_id)
    return obj if obj and obj.get("status") == "approved" else None


def extraction_gate_error(source):
    """D-02/D-03/D-04 require target-expert confirmation before LLM use."""
    db = _load_db()
    task_id = source.get("task_id", "")
    if not source.get("target_expert"):
        return {"error": "gate_required", "gate_id": "G1", "message": "目标专家确认前不能执行 LLM 提炼"}
    if not approved_gate(db, task_id, "G1"):
        return {"error": "gate_required", "gate_id": "G1", "message": "目标专家尚未通过 G1"}
    return None


# ── D-03: LLM 辅助策略结构提炼 ────────────────────
def llm_extract_strategy(source_id, max_utts=20, include_all=False):
    """用 LLM 从证据中提议策略节点、边、候选人状态和触发条件"""
    db = _load_db()
    source = db["sources"].get(source_id)
    if not source:
        return {"error": "source_not_found"}
    gate_error = extraction_gate_error(source)
    if gate_error:
        return gate_error

    all_evidence = [e for e in db["evidence"] if e.get("source_id") == source_id]
    evidence = [e for e in all_evidence
                if _is_target_expert_evidence(source, e)
                and (include_all or e.get("evidence_kind") in ("strategy", "script"))]
    if not evidence:
        return {"error": "no_eligible_evidence", "message": "没有已归类的目标专家策略/话术证据"}

    baseline, baseline_error = _selected_baseline(db, source)
    if baseline_error:
        return baseline_error
    requested_max_utts = max_utts
    if include_all:
        max_utts = len(all_evidence)
        evs_to_send = all_evidence
    else:
        max_utts = _effective_max_utts(max_utts)
        evs_to_send = _context_window_evidence(all_evidence, evidence, max_utts)
    support_refs = [e.get("evidence_id") for e in evs_to_send if _is_target_expert_evidence(source, e)]
    context_refs = [e.get("evidence_id") for e in evs_to_send if not _is_target_expert_evidence(source, e)]
    output_max_tokens = 65536
    run_options = {
        "requested_max_utts": requested_max_utts,
        "effective_max_utts": max_utts,
        "include_all": include_all,
        "output_max_tokens": output_max_tokens,
        "thinking": "enabled" if include_all else "default",
        "support_evidence_refs": support_refs,
        "context_refs": context_refs,
        "prompt_version": STRATEGY_PROMPT_VERSION,
        "schema_version": ANALYSIS_SCHEMA_VERSION,
    }
    input_fingerprint = _analysis_input_fingerprint(
        source, "strategy_extraction", evidence=evs_to_send,
        run_options=run_options, baseline=baseline
    )
    previous_run = _find_completed_analysis_run(source_id, "strategy_extraction", input_fingerprint)
    if previous_run:
        return _reuse_analysis_run(previous_run)

    task_id = source.get("task_id", "")
    expert_name = source.get("target_expert", "")
    baseline_payload = None
    if baseline:
        baseline_payload = {
            "baseline_id": baseline.get("baseline_id"),
            "version": baseline.get("version"),
            "content_hash": baseline.get("content_hash"),
            "graph": baseline.get("graph") or {},
        }
    evidence_payload = [_evidence_prompt_item(item, source) for item in evs_to_send]
    user_content = (
        "## EXISTING_BASELINE\n"
        + json.dumps(baseline_payload, ensure_ascii=False, indent=2)
        + "\n\n## NEW_INTERVIEW_EVIDENCE\n"
        + json.dumps(evidence_payload, ensure_ascii=False, indent=2)
    )
    system_prompt = f"""你是猎头策略 Graph 的增量变更分析器。目标专家是：{expert_name}。

任务不是重新画一张独立流程图，而是把 NEW_INTERVIEW_EVIDENCE 对 EXISTING_BASELINE 的最小、可审计变更表达出来。先完整理解基线的节点、边、条件和节点话术，再分析新访谈会修改、拆分、合并、废弃或新增什么。基线已有且访谈没有改变的内容不要重复输出。

证据归属硬约束：
1. 只有 role=SUPPORT（目标专家本人亲口表达）的 evidence_id/utterance_id 可以进入 evidence_refs 和 script_evidence_refs。
2. role=CONTEXT 只能进入 context_refs，绝不能把访谈者或其他专家的策略算到目标专家头上。
3. 目标专家明确认可他人说法时，evidence_refs 放专家本人的认可发言，context_refs 放被认可原话；孤立的“对/嗯/是”不得单独形成策略。
4. script_evidence_refs 只标记需要按原话保存的目标专家发言。原话内容由服务端按证据 ID 读取，不要改写或臆造引文。
5. 不得补全访谈没有讲过的节点、边或条件；不确定就写入 uncertainties。

增量变更硬约束：
- change_type 只能是 add、modify、deprecate、split、merge、keep。
- add 的 baseline_refs 必须为空。
- modify/deprecate/split/keep 必须引用精确的基线实体 ID；merge 必须引用至少两个基线实体 ID。
- candidate_key 是本次候选中的稳定局部 ID。边的 from_ref/to_ref 可以引用 candidate_key，也可以直接引用未修改的基线 node ID。
- reason 要说明新证据为什么导致该变更。baseline_refs 禁止填 label，必须填 EXISTING_BASELINE 中的精确 ID。

只返回一个 JSON 对象，不要 Markdown，不要解释文字，结构如下：
{{
  "analysis_summary": "",
  "nodes": [{{
    "candidate_key": "node_change_1",
    "change_type": "add|modify|deprecate|split|merge|keep",
    "baseline_refs": ["baseline_node_id"],
    "node_name": "",
    "reason": "",
    "evidence_refs": ["evidence_id"],
    "context_refs": ["evidence_id"],
    "script_evidence_refs": ["evidence_id"],
    "is_fragment": false
  }}],
  "edges": [{{
    "candidate_key": "edge_change_1",
    "change_type": "add|modify|deprecate|split|merge|keep",
    "baseline_refs": ["baseline_edge_id"],
    "from_ref": "candidate_key_or_baseline_node_id",
    "to_ref": "candidate_key_or_baseline_node_id",
    "condition": "候选人状态或分支条件",
    "condition_uncertainty": "可选；仅在条件确实存在不确定性时说明，不得把未穷尽边界写成否定",
    "reason": "",
    "evidence_refs": ["evidence_id"],
    "context_refs": ["evidence_id"]
  }}],
  "triggers": [{{
    "candidate_key": "trigger_change_1",
    "change_type": "add|modify|deprecate|split|merge|keep",
    "baseline_refs": ["baseline_trigger_id"],
    "condition": "",
    "target_ref": "candidate_key_or_baseline_node_id",
    "reason": "",
    "evidence_refs": ["evidence_id"],
    "context_refs": ["evidence_id"]
  }}],
  "candidate_states": [""],
  "uncertainties": [""]
}}"""

    if include_all:
        system_prompt = "输入包含完整访谈与完整基线，禁止为了简短而省略有证据支持的增量变化。\n" + system_prompt

    if not llm_client:
        return {"error": "llm_client_not_available"}

    result = llm_client.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ], max_tokens=output_max_tokens, **({"thinking": {"type": "enabled"}} if include_all else {}))

    if "error" in result:
        return {"error": "llm_call_failed", "detail": result}

    raw_content = result.get("content", "")
    import json as _json
    structure = None
    try:
        start = raw_content.find("{")
        end = raw_content.rfind("}")
        if start >= 0 and end > start:
            structure = _json.loads(raw_content[start:end + 1])
        else:
            return {"error": "llm_parse_failed", "raw": raw_content[:500]}
    except _json.JSONDecodeError as e:
        return {"error": "llm_json_error", "message": str(e), "raw": raw_content[:500]}

    # 将 LLM 提议的节点存为候选知识对象，并把图关系持久化为候选对象。
    run = _create_analysis_run(source, "strategy_extraction", input_fingerprint, dict(
        run_options,
        baseline_id=baseline.get("baseline_id") if baseline else None,
        baseline_version=baseline.get("version") if baseline else None,
        baseline_content_hash=baseline.get("content_hash") if baseline else None,
    ))
    run_id = run["run_id"]
    group = create_knowledge_object(
        task_id=source.get("task_id", ""),
        source_id=source_id,
        obj_type="strategy_script_group",
        content="候选专家策略话术组：" + source.get("filename", source_id),
        evidence_refs=[],
        scope="general",
        linkage={
            "expert_id": source.get("target_expert"),
            "source_id": source_id,
            "baseline_id": baseline.get("baseline_id") if baseline else None,
            "baseline_version": baseline.get("version") if baseline else None,
            "baseline_content_hash": baseline.get("content_hash") if baseline else None,
            "change_schema_version": ANALYSIS_SCHEMA_VERSION,
        },
        analysis_run_id=run_id,
    )
    if "error" in group:
        return group
    group_id = group["object_id"]
    nodes_created = []
    rejected_nodes = []
    task_id = source.get("task_id", "")
    baseline_indexes = _baseline_indexes(baseline)
    baseline_node_ids = set(baseline_indexes["nodes"]["by_id"])
    for node in structure.get("nodes", []):
        baseline_refs, invalid_baseline_refs = _normalize_baseline_refs(
            node.get("baseline_refs"), node.get("baseline_match"), baseline_indexes["nodes"]
        )
        if invalid_baseline_refs:
            rejected_nodes.append({"node": node, "reason": "invalid_baseline_ref", "refs": invalid_baseline_refs})
            continue
        change_type = _normalize_change_type(node.get("change_type"), baseline_refs, bool(baseline))
        contract_error = _change_contract_error(change_type, baseline_refs, bool(baseline))
        if contract_error:
            rejected_nodes.append({"node": node, "reason": contract_error})
            continue
        ev_refs = normalize_evidence_refs(db, node.get("evidence_refs", []))
        node_context_refs = node.get("context_refs", [])
        attribution_error = _expert_support_error(db, source, ev_refs, node_context_refs)
        if attribution_error:
            rejected_nodes.append({"node": node, "reason": attribution_error["error"]})
            continue
        script_evidence_refs = normalize_evidence_refs(db, node.get("script_evidence_refs", []))
        script_error = None
        if script_evidence_refs:
            unknown_script_refs = validate_evidence_refs(db, script_evidence_refs)
            script_items = [_evidence_for(db, ref) for ref in script_evidence_refs]
            if unknown_script_refs or any(not _is_target_expert_evidence(source, item or {}) for item in script_items):
                script_error = "invalid_script_evidence_ref"
        if script_error:
            rejected_nodes.append({"node": node, "reason": script_error})
            continue
        node_name = str(node.get("node_name") or "").strip()
        if not node_name and baseline_refs:
            node_name = str(baseline_indexes["nodes"]["by_id"][baseline_refs[0]].get("label") or baseline_refs[0])
        ko = create_knowledge_object(
            task_id=task_id,
            source_id=source_id,
            obj_type="strategy_node",
            content=node_name,
            evidence_refs=ev_refs,
            scope="general",
            linkage={
                "is_fragment": node.get("is_fragment", True),
                "group_id": group_id,
                "candidate_key": node.get("candidate_key") or _new_id("candidate_node"),
                "change_type": change_type,
                "baseline_id": baseline.get("baseline_id") if baseline else None,
                "baseline_version": baseline.get("version") if baseline else None,
                "baseline_content_hash": baseline.get("content_hash") if baseline else None,
                "baseline_refs": baseline_refs,
                "baseline_match": baseline_refs[0] if len(baseline_refs) == 1 else None,
                "change_reason": node.get("reason", ""),
                "script_evidence_refs": script_evidence_refs,
                "context_refs": normalize_evidence_refs(db, node_context_refs),
                "evidence_mode": "endorsement" if node_context_refs else "direct",
            },
            analysis_run_id=run_id,
        )
        if "error" in ko:
            continue
        nodes_created.append({
            "object_id": ko["object_id"],
            "candidate_key": ko.get("linkage", {}).get("candidate_key"),
            "node_name": node_name,
            "change_type": change_type,
            "baseline_refs": baseline_refs,
            "script_evidence_refs": script_evidence_refs,
            "is_fragment": node.get("is_fragment", True),
            "evidence_refs": ev_refs,
        })

    node_ids = {}
    for item in nodes_created:
        node_ids[item["node_name"]] = item["object_id"]
        node_ids[item["candidate_key"]] = item["object_id"]
    node_ids.update({node_id: node_id for node_id in baseline_node_ids})
    edges_created = []
    rejected_edges = []
    for edge in structure.get("edges", []):
        baseline_refs, invalid_baseline_refs = _normalize_baseline_refs(
            edge.get("baseline_refs"), edge.get("baseline_match"), baseline_indexes["edges"]
        )
        if invalid_baseline_refs:
            rejected_edges.append({"edge": edge, "reason": "invalid_baseline_ref", "refs": invalid_baseline_refs})
            continue
        change_type = _normalize_change_type(edge.get("change_type"), baseline_refs, bool(baseline))
        contract_error = _change_contract_error(change_type, baseline_refs, bool(baseline))
        if contract_error:
            rejected_edges.append({"edge": edge, "reason": contract_error})
            continue
        baseline_edge = baseline_indexes["edges"]["by_id"].get(baseline_refs[0]) if len(baseline_refs) == 1 else None
        from_ref = edge.get("from_ref") or edge.get("from") or edge.get("from_node_id") or (baseline_edge or {}).get("source")
        to_ref = edge.get("to_ref") or edge.get("to") or edge.get("to_node_id") or (baseline_edge or {}).get("target")
        from_id = node_ids.get(from_ref, "")
        to_id = node_ids.get(to_ref, "")
        if not from_id or not to_id:
            rejected_edges.append({"edge": edge, "reason": "invalid_endpoint_ref", "from_ref": from_ref, "to_ref": to_ref})
            continue
        ev_refs = normalize_evidence_refs(db, edge.get("evidence_refs", []))
        edge_context_refs = edge.get("context_refs", [])
        attribution_error = _expert_support_error(db, source, ev_refs, edge_context_refs)
        if attribution_error:
            rejected_edges.append({"edge": edge, "reason": attribution_error["error"]})
            continue
        edge_obj = create_knowledge_object(
            task_id=task_id, source_id=source_id, obj_type="strategy_edge",
            content=edge.get("condition", ""), evidence_refs=ev_refs, scope="general",
            linkage={
                "group_id": group_id,
                "candidate_key": edge.get("candidate_key") or _new_id("candidate_edge"),
                "change_type": change_type,
                "baseline_id": baseline.get("baseline_id") if baseline else None,
                "baseline_version": baseline.get("version") if baseline else None,
                "baseline_content_hash": baseline.get("content_hash") if baseline else None,
                "baseline_refs": baseline_refs,
                "from_ref": from_ref,
                "to_ref": to_ref,
                "from_node_id": from_id,
                "to_node_id": to_id,
                "condition": edge.get("condition", ""),
                "extracted_condition": edge.get("condition", ""),
                "condition_review_status": "needs_review" if edge.get("condition_uncertainty") else "unreviewed",
                "condition_uncertainty": edge.get("condition_uncertainty", ""),
                "change_reason": edge.get("reason", ""),
                "context_refs": normalize_evidence_refs(db, edge_context_refs),
                "evidence_mode": "endorsement" if edge_context_refs else "direct",
            },
            analysis_run_id=run_id,
        )
        if "error" in edge_obj:
            rejected_edges.append({"edge": edge, "reason": edge_obj["error"]})
        else:
            edges_created.append(edge_obj)
    triggers_created = []
    rejected_triggers = []
    for trigger in structure.get("triggers", []):
        trigger_text = trigger if isinstance(trigger, str) else trigger.get("condition", "")
        trigger = {"condition": trigger_text, "change_type": "add"} if isinstance(trigger, str) else trigger
        baseline_refs, invalid_baseline_refs = _normalize_baseline_refs(
            trigger.get("baseline_refs"), trigger.get("baseline_match"), baseline_indexes["triggers"]
        )
        if invalid_baseline_refs:
            rejected_triggers.append({"trigger": trigger, "reason": "invalid_baseline_ref", "refs": invalid_baseline_refs})
            continue
        change_type = _normalize_change_type(trigger.get("change_type"), baseline_refs, bool(baseline))
        contract_error = _change_contract_error(change_type, baseline_refs, bool(baseline))
        if contract_error:
            rejected_triggers.append({"trigger": trigger, "reason": contract_error})
            continue
        baseline_trigger = baseline_indexes["triggers"]["by_id"].get(baseline_refs[0]) if len(baseline_refs) == 1 else None
        target_ref = trigger.get("target_ref") or trigger.get("target_node") or trigger.get("target_node_id") or (baseline_trigger or {}).get("target_node_id")
        target_id = node_ids.get(target_ref, "")
        if target_ref and not target_id:
            rejected_triggers.append({"trigger": trigger, "reason": "invalid_endpoint_ref", "target_ref": target_ref})
            continue
        ev_refs = [] if isinstance(trigger, str) else trigger.get("evidence_refs", [])
        ev_refs = normalize_evidence_refs(db, ev_refs)
        trigger_context_refs = trigger.get("context_refs", [])
        attribution_error = _expert_support_error(db, source, ev_refs, trigger_context_refs)
        if attribution_error:
            rejected_triggers.append({"trigger": trigger, "reason": attribution_error["error"]})
            continue
        trigger_obj = create_knowledge_object(
            task_id=task_id, source_id=source_id, obj_type="strategy_trigger",
            content=trigger_text, evidence_refs=ev_refs, scope="general",
            linkage={
                "group_id": group_id,
                "candidate_key": trigger.get("candidate_key") or _new_id("candidate_trigger"),
                "change_type": change_type,
                "baseline_id": baseline.get("baseline_id") if baseline else None,
                "baseline_version": baseline.get("version") if baseline else None,
                "baseline_content_hash": baseline.get("content_hash") if baseline else None,
                "baseline_refs": baseline_refs,
                "target_ref": target_ref,
                "target_node_id": target_id,
                "condition": trigger_text,
                "change_reason": trigger.get("reason", ""),
                "context_refs": normalize_evidence_refs(db, trigger_context_refs),
                "evidence_mode": "endorsement" if trigger_context_refs else "direct",
            },
            analysis_run_id=run_id,
        )
        if "error" in trigger_obj:
            rejected_triggers.append({"trigger": trigger, "reason": trigger_obj["error"]})
        else:
            triggers_created.append(trigger_obj)
    states_created = structure.get("candidate_states", [])
    graph_refs = [ref for item in nodes_created for ref in item.get("evidence_refs", [])]
    graph_refs += [ref for item in edges_created for ref in item.get("evidence_refs", [])]
    graph_refs += [ref for item in triggers_created for ref in item.get("evidence_refs", [])]
    rejected_changes = []
    for entity_type, rejected in (
            ("node", rejected_nodes), ("edge", rejected_edges), ("trigger", rejected_triggers)):
        for item in rejected:
            raw_change = item.get(entity_type) or {}
            rejected_changes.append({
                "entity_type": entity_type,
                "candidate_key": raw_change.get("candidate_key"),
                "change_type": raw_change.get("change_type"),
                "reason": item.get("reason"),
                "baseline_refs": raw_change.get("baseline_refs") or [],
                "endpoint_refs": [ref for ref in (
                    raw_change.get("from_ref"), raw_change.get("to_ref"), raw_change.get("target_ref")
                ) if ref],
            })
    graph_obj = create_knowledge_object(
        task_id=task_id, source_id=source_id, obj_type="graph",
        content="候选策略流程图", evidence_refs=sorted(set(graph_refs)), scope="general",
        linkage={
            "group_id": group_id,
            "node_ids": [item["object_id"] for item in nodes_created],
            "edge_ids": [item["object_id"] for item in edges_created],
            "trigger_ids": [item["object_id"] for item in triggers_created],
            "is_fragment": not bool(edges_created),
            "baseline_id": baseline.get("baseline_id") if baseline else None,
            "baseline_version": baseline.get("version") if baseline else None,
            "baseline_content_hash": baseline.get("content_hash") if baseline else None,
            "change_schema_version": ANALYSIS_SCHEMA_VERSION,
            "analysis_summary": structure.get("analysis_summary", ""),
            "candidate_states": states_created,
            "uncertainties": structure.get("uncertainties", []),
            "rejected_changes": rejected_changes,
        },
        analysis_run_id=run_id,
    )

    response = {
        "source_id": source_id,
        "nodes_created": nodes_created,
        "rejected_nodes": rejected_nodes,
        "group_id": group_id,
        "graph_id": graph_obj.get("object_id") if "error" not in graph_obj else None,
        "edges": edges_created,
        "triggers": triggers_created,
        "rejected_edges": rejected_edges,
        "rejected_triggers": rejected_triggers,
        "candidate_states": states_created,
        "analysis_summary": structure.get("analysis_summary", ""),
        "uncertainties": structure.get("uncertainties", []),
        "llm_model": result.get("model", ""),
        "llm_usage": result.get("usage", {}),
        "run_id": run_id,
        "deduplicated": False,
        "requested_max_utts": requested_max_utts,
        "effective_max_utts": max_utts,
    }
    _finish_analysis_run(run_id, response)
    return response


# ── D-04: LLM 辅助话术映射 ────────────────────────
def llm_map_scripts(source_id, max_utts=15):
    """用 LLM 将专家原话映射到策略节点，判断话术类型"""
    db = _load_db()
    source = db["sources"].get(source_id)
    if not source:
        return {"error": "source_not_found"}
    gate_error = extraction_gate_error(source)
    if gate_error:
        return gate_error

    all_evidence = [e for e in db["evidence"] if e.get("source_id") == source_id]
    evidence = [e for e in all_evidence
                if e.get("evidence_kind") in ("script", "strategy")
                and _is_target_expert_evidence(source, e)]

    knowledge = [o for o in db.get("knowledge", {}).values()
                 if o.get("source_id") == source_id and o.get("type") == "strategy_node"
                 and o.get("status") != "archived"]

    if not evidence or not knowledge:
        return {"error": "no_evidence_or_nodes",
                "evidence_count": len(evidence),
                "knowledge_count": len(knowledge)}

    requested_max_utts = max_utts
    max_utts = _effective_max_utts(max_utts)
    evs_to_send = _context_window_evidence(all_evidence, evidence, max_utts)
    run_options = {
        "requested_max_utts": requested_max_utts,
        "effective_max_utts": max_utts,
        "support_evidence_refs": [e.get("evidence_id") for e in evs_to_send if _is_target_expert_evidence(source, e)],
        "context_refs": [e.get("evidence_id") for e in evs_to_send if not _is_target_expert_evidence(source, e)],
        "prompt_version": SCRIPT_PROMPT_VERSION,
        "schema_version": ANALYSIS_SCHEMA_VERSION,
    }
    baseline, baseline_error = _selected_baseline(db, source)
    if baseline_error:
        return baseline_error
    input_fingerprint = _analysis_input_fingerprint(
        source, "script_mapping", evidence=evs_to_send, knowledge=knowledge,
        run_options=run_options, baseline=baseline
    )
    previous_run = _find_completed_analysis_run(source_id, "script_mapping", input_fingerprint)
    if previous_run:
        return _reuse_analysis_run(previous_run)

    ev_lines = []
    for e in evs_to_send:
        role = "SUPPORT" if _is_target_expert_evidence(source, e) else "CONTEXT"
        ev_lines.append("[{}][{}] {}: {}".format(role, e.get("utterance_id", ""), e.get("speaker", ""), e.get("content", "")[:200]))

    node_lines = []
    for k in knowledge:
        node_lines.append("[{}] {}".format(k["object_id"], k.get("content", "")))

    expert_name = source.get("target_expert", "")
    system_prompt = (
        "你是一个猎头话术分析助手。目标专家是：" + expert_name + "。下面有策略节点列表和发言列表。\n"
        "请将每条发言映射到最相关的策略节点，并判断话术类型。\n"
        "重要：只将目标专家（" + expert_name + "）亲口说的话术映射到节点。其他人的发言不映射，直接跳过。\n"
        "- direct_script: 目标专家说了可直接用于电话的原话话术\n"
        "- partial_script: 只有承接词、片段或通话后动作承诺\n"
        "- strategy_only: 目标专家讲清了动作逻辑，但没有可独立使用的候选人句子\n"
        "- no_script: 该项实际是候选人状态或触发条件，无猎头话术\n"
        "以 JSON 数组返回：[{\"utterance_id\":\"\",\"node_id\":\"\",\"script_type\":\"\",\"reason\":\"\"}]\n"
        "只返回 JSON，不要其他文字。"
    )

    user_content = "策略节点：\n" + "\n".join(node_lines) + "\n\n发言列表：\n" + "\n".join(ev_lines)

    if not llm_client:
        return {"error": "llm_client_not_available"}

    result = llm_client.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ], max_tokens=65536)

    if "error" in result:
        return {"error": "llm_call_failed", "detail": result}

    raw_content = result.get("content", "")
    import json as _json
    mappings = []
    try:
        start = raw_content.find("[")
        end = raw_content.rfind("]")
        if start >= 0 and end > start:
            mappings = _json.loads(raw_content[start:end + 1])
        else:
            return {"error": "llm_parse_failed", "raw": raw_content[:500]}
    except _json.JSONDecodeError as e:
        return {"error": "llm_json_error", "message": str(e), "raw": raw_content[:500]}

    # 为有 direct_script/partial_script 的创建候选话术对象
    run = _create_analysis_run(source, "script_mapping", input_fingerprint, dict(
        run_options,
        baseline_id=baseline.get("baseline_id") if baseline else None,
        baseline_version=baseline.get("version") if baseline else None,
        baseline_content_hash=baseline.get("content_hash") if baseline else None,
    ))
    run_id = run["run_id"]
    scripts_created = []
    rejected_mappings = []
    task_id = source.get("task_id", "")
    for m in mappings:
        uid = m.get("utterance_id", "")
        node_id = m.get("node_id", "")
        script_type = m.get("script_type", "no_script")
        reason = m.get("reason", "")

        # 找对应的证据
        ev = None
        for e in db["evidence"]:
            if e.get("utterance_id") == uid:
                ev = e
                break

        if script_type in ("direct_script", "partial_script") and ev and node_id:
            if not _is_target_expert_evidence(source, ev):
                rejected_mappings.append({"mapping": m, "reason": "target_expert_evidence_required"})
                continue
            if _is_terse_endorsement(ev.get("content")):
                rejected_mappings.append({"mapping": m, "reason": "endorsement_is_not_direct_script"})
                continue
            node = db.get("knowledge", {}).get(node_id)
            if not node or node.get("task_id") != task_id or node.get("type") != "strategy_node":
                rejected_mappings.append({"mapping": m, "reason": "invalid_strategy_node"})
                continue
            spt = create_knowledge_object(
                task_id=task_id,
                source_id=source_id,
                obj_type="script_fragment",
                content=ev.get("content", ""),
                evidence_refs=[ev.get("evidence_id")],
                scope="general",
                linkage={"node_id": node_id, "script_type": script_type},
                analysis_run_id=run_id,
            )
            scripts_created.append({
                "object_id": spt["object_id"],
                "utterance_id": uid,
                "node_id": node_id,
                "script_type": script_type,
                "reason": reason,
                "status": spt.get("status"),
                "duplicate_of": spt.get("duplicate_of"),
            })

    response = {
        "source_id": source_id,
        "total_mappings": len(mappings),
        "scripts_created": scripts_created,
        "rejected_mappings": rejected_mappings,
        "all_mappings": mappings,
        "llm_model": result.get("model", ""),
        "llm_usage": result.get("usage", {}),
        "run_id": run_id,
        "deduplicated": False,
        "requested_max_utts": requested_max_utts,
        "effective_max_utts": max_utts,
    }
    _finish_analysis_run(run_id, response)
    return response


# ── D-05: Gate 状态机 G1-G5 ───────────────────────
GATE_DEFS = {
    "G1": {"name": "目标专家确认", "roles": ["operator", "reviewer"]},
    "G2": {"name": "证据归类确认", "roles": ["reviewer"]},
    "G3": {"name": "知识变更审核", "roles": ["reviewer"]},
    "G4": {"name": "高风险政策确认", "roles": ["policy_owner"]},
    "G5": {"name": "发布确认", "roles": ["release_owner"]},
}

GATE_RESULTS = ["approved", "rejected", "request_modification", "archive", "auto_pass"]


def gate_action(gate_id, task_id, reviewer, decision, reason, target_object_id=None,
                evidence_refs=None, before_obj=None, after_obj=None,
                target_expert=None, baseline_id=None):
    """执行 Gate 审批操作"""
    db = _load_db()
    if "gates" not in db:
        db["gates"] = []

    if gate_id not in GATE_DEFS:
        return {"error": "invalid_gate", "allowed": list(GATE_DEFS.keys())}

    if decision not in GATE_RESULTS:
        return {"error": "invalid_decision", "allowed": GATE_RESULTS}

    task = _task_for(db, task_id)
    if not task:
        return {"error": "task_not_found"}
    expected_current = {"G2": "G2", "G3": "G3", "G4": "G4", "G5": "G5"}.get(gate_id)
    if (decision == "approved" and expected_current
            and task.get("current_gate") != expected_current):
        return {"error": "gate_not_current", "gate_id": gate_id,
                "current_gate": task.get("current_gate")}
    prior_gate = {"G2": "G1", "G3": "G2", "G4": "G3", "G5": "G3"}.get(gate_id)
    if decision == "approved" and prior_gate and not approved_gate(db, task_id, prior_gate):
        return {"error": "gate_required", "gate_id": prior_gate,
                "message": "previous gate must be approved first"}
    if gate_id == "G1" and decision == "approved" and not target_expert:
        return {"error": "missing_target_expert"}
    if gate_id == "G1" and decision == "approved" and baseline_id:
        baseline = next((item for item in db.get("graph_baselines", [])
                         if item.get("baseline_id") == baseline_id
                         and item.get("task_id") == task_id), None)
        if not baseline:
            return {"error": "baseline_not_found", "baseline_id": baseline_id}
        if baseline.get("content_hash") != _stable_fingerprint(baseline.get("graph", {})):
            return {"error": "baseline_hash_mismatch", "baseline_id": baseline_id}
    if gate_id == "G2" and decision == "approved":
        refs = normalize_evidence_refs(db, evidence_refs or [])
        if not refs:
            return {"error": "missing_evidence", "gate_id": "G2"}
        unknown = validate_evidence_refs(db, refs)
        if unknown:
            return {"error": "unknown_evidence_ref", "evidence_refs": unknown}
        invalid = []
        for ref in refs:
            evidence = _evidence_for(db, ref) or {}
            reason_code = None
            if evidence.get("task_id") != task_id:
                reason_code = "cross_task_evidence"
            elif evidence.get("evidence_kind") not in VALID_EVIDENCE_KINDS:
                reason_code = "invalid_evidence_kind"
            if reason_code:
                invalid.append({"evidence_id": ref, "status": "blocked", "reason": reason_code})
        if invalid:
            return {"error": invalid[0]["reason"], "gate_id": "G2", "evidence_results": invalid}

    # 检查是否有责任人（auto_pass 除外）
    if not reviewer and decision != "auto_pass":
        return {"error": "missing_reviewer", "message": "Gate 审批必须记录责任人"}

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    audit_id = _new_id("aud")

    gate_record = {
        "audit_id": audit_id,
        "gate_id": gate_id,
        "task_id": task_id,
        "reviewer": reviewer,
        "reviewer_role": GATE_DEFS[gate_id]["name"],
        "decision": decision,
        "reason": reason,
        "target_object_id": target_object_id,
        "evidence_refs": normalize_evidence_refs(db, evidence_refs or []),
        "before_object": before_obj,
        "after_object": after_obj,
        "created_at": now,
        "immutable": True,
    }
    if target_expert:
        gate_record["target_expert"] = target_expert

    db["gates"].append(gate_record)

    if gate_id == "G1" and decision == "approved":
        old_expert = task.get("target_expert")
        old_baseline = task.get("baseline_id")
        confirmation_version = int(task.get("target_expert_confirmation_version", 0)) + 1
        task["target_expert"] = target_expert
        task["target_expert_confirmation_version"] = confirmation_version
        task["current_gate"] = "G2"
        if baseline_id:
            task["baseline_id"] = baseline_id
        source = db.get("sources", {}).get(task.get("source_id"))
        if source:
            source["target_expert"] = target_expert
            source["target_expert_confirmation_version"] = confirmation_version
            if baseline_id:
                source["baseline_id"] = baseline_id
        if old_expert != target_expert or old_baseline != baseline_id:
            for run in db.get("analysis_runs", []):
                if run.get("task_id") == task_id and run.get("status") == "completed":
                    run["status"] = "invalidated"
                    run["invalidated_reason"] = "target_expert_or_baseline_changed"
                    run["invalidated_at"] = now
    elif gate_id == "G2" and decision == "approved":
        accepted = normalize_evidence_refs(db, evidence_refs or [])
        for ev in db.get("evidence", []):
            if ev.get("evidence_id") in accepted:
                ev["status"] = "approved"
                ev["reviewed_by"] = reviewer
                ev["reviewed_at"] = now
        task["current_gate"] = "G3"
    if gate_id == "G3" and decision == "approved" and target_object_id:
        obj = db.get("knowledge", {}).get(target_object_id)
        if not obj:
            return {"error": "target_not_found"}
        if obj.get("status") not in ("candidate", "pending_review"):
            return {"error": "invalid_target_status"}
        refs = normalize_evidence_refs(db, obj.get("evidence_refs", []))
        if not refs:
            return {"error": "missing_evidence", "gate_id": "G3"}
        if any((_evidence_for(db, ref) or {}).get("status") != "approved" for ref in refs):
            return {"error": "gate_required", "gate_id": "G2",
                    "message": "all object evidence must pass G2"}
        source = db.get("sources", {}).get(obj.get("source_id"))
        attribution_error = _expert_support_error(
            db, source or {}, refs, obj.get("linkage", {}).get("context_refs", [])
        )
        if attribution_error:
            return attribution_error
        obj["status"] = "approved"
        obj["immutable"] = True
        obj["updated_at"] = now
        remaining = [item for item in db.get("knowledge", {}).values()
                     if item.get("task_id") == task_id
                     and item.get("status") in ("candidate", "pending_review")
                     and item.get("type") not in ("expert", "strategy_script_group")]
        needs_g4 = any(item.get("task_id") == task_id
                       and item.get("type") in ("policy_guard", "scoring_rule")
                       and item.get("status") != "archived"
                       for item in db.get("knowledge", {}).values())
        task["current_gate"] = "G3" if remaining else ("G4" if needs_g4 else "G5")
        task["last_completed_gate"] = "G3"
    elif gate_id == "G4" and decision == "approved":
        task["current_gate"] = "G5"
    elif gate_id == "G5" and decision == "approved":
        task["current_gate"] = "published"
    task["updated_at"] = now
    _save_db(db)
    return gate_record


def list_gates(task_id=None, gate_id=None, target_object_id=None):
    """查询 Gate 记录"""
    db = _load_db()
    result = db.get("gates", [])
    if task_id:
        result = [g for g in result if g.get("task_id") == task_id]
    if gate_id:
        result = [g for g in result if g.get("gate_id") == gate_id]
    if target_object_id:
        result = [g for g in result if g.get("target_object_id") == target_object_id]
    return result


def _script_graph_context(db, task_id, graph_id, node_id, editable=False):
    task = _task_for(db, task_id)
    graph = db.get("knowledge", {}).get(graph_id)
    node = db.get("knowledge", {}).get(node_id)
    if not task:
        return None, {"error": "task_not_found"}
    if (not graph or graph.get("task_id") != task_id or graph.get("type") != "graph"):
        return None, {"error": "graph_not_found"}
    if (not node or node.get("task_id") != task_id or node.get("type") != "strategy_node"
            or node_id not in (graph.get("linkage", {}).get("node_ids") or [])):
        return None, {"error": "graph_node_not_found"}
    if editable and (graph.get("status") == "approved" or node.get("status") == "approved"):
        return None, {"error": "approved_graph_immutable", "message": "已批准 Graph 的话术版本只读"}
    source = db.get("sources", {}).get(node.get("source_id") or graph.get("source_id")) or {}
    return {"task": task, "graph": graph, "node": node, "source": source}, None


def _resolve_node_script_examples(db, node, graph_id=None):
    """Resolve selected original/corrected scripts without mutating evidence."""
    linkage = node.get("linkage") or {}
    refs = normalize_evidence_refs(db, linkage.get("script_evidence_refs", []))
    raw = linkage.get("script_selections")
    selections = ([{"evidence_id": ref, "variant_id": None} for ref in refs]
                  if raw is None else raw)
    if not isinstance(selections, list):
        return [], [{"error": "invalid_script_selections"}]
    evidence_by_id = {item.get("evidence_id"): item for item in db.get("evidence", [])}
    source = db.get("sources", {}).get(node.get("source_id")) or {}
    examples, issues, seen = [], [], set()
    for selection in selections:
        if not isinstance(selection, dict):
            issues.append({"error": "invalid_script_selection"})
            continue
        evidence_id = str(selection.get("evidence_id") or "")
        variant_id = selection.get("variant_id") or None
        if evidence_id in seen:
            issues.append({"error": "duplicate_script_selection", "evidence_id": evidence_id})
            continue
        seen.add(evidence_id)
        evidence = evidence_by_id.get(evidence_id)
        if evidence_id not in refs or not evidence or evidence.get("task_id") != node.get("task_id"):
            issues.append({"error": "invalid_script_evidence", "evidence_id": evidence_id})
            continue
        if not _is_target_expert_evidence(source, evidence):
            issues.append({"error": "target_expert_evidence_required", "evidence_id": evidence_id})
            continue
        text = evidence.get("content", "")
        version_kind = "source"
        if variant_id:
            variant = db.get("knowledge", {}).get(variant_id)
            variant_linkage = (variant or {}).get("linkage") or {}
            if (not variant or variant.get("type") != "script_fragment"
                    or variant.get("task_id") != node.get("task_id")
                    or variant_linkage.get("editor_variant") is not True
                    or variant_linkage.get("node_id") != node.get("object_id")
                    or variant_linkage.get("source_evidence_id") != evidence_id
                    or (graph_id and variant_linkage.get("graph_id") != graph_id)
                    or variant.get("status") in ("archived", "rejected")):
                issues.append({"error": "invalid_script_variant", "evidence_id": evidence_id,
                               "variant_id": variant_id})
                continue
            text = variant.get("content", "")
            version_kind = "manual_corrected"
        examples.append({
            "evidence_id": evidence_id,
            "version_id": variant_id,
            "version_kind": version_kind,
            "speaker": evidence.get("speaker"),
            "timestamp": evidence.get("timestamp"),
            "utterance_id": evidence.get("utterance_id"),
            "text": text,
            "evidence_text": evidence.get("content", ""),
        })
    return examples, issues


def get_node_script_workspace(task_id, graph_id, node_id):
    db = _load_db()
    context, error = _script_graph_context(db, task_id, graph_id, node_id)
    if error:
        return error
    node, graph = context["node"], context["graph"]
    examples, issues = _resolve_node_script_examples(db, node, graph_id)
    if issues:
        return {"error": "invalid_script_selections", "issues": issues}
    selected = {item["evidence_id"]: item.get("version_id") for item in examples}
    refs = normalize_evidence_refs(db, (node.get("linkage") or {}).get("script_evidence_refs", []))
    evidence_by_id = {item.get("evidence_id"): item for item in db.get("evidence", [])}
    variants = [item for item in db.get("knowledge", {}).values()
                if item.get("type") == "script_fragment"
                and item.get("task_id") == task_id
                and item.get("status") != "archived"
                and (item.get("linkage") or {}).get("editor_variant") is True
                and (item.get("linkage") or {}).get("graph_id") == graph_id
                and (item.get("linkage") or {}).get("node_id") == node_id]
    variants.sort(key=lambda item: (item.get("created_at", ""), item.get("object_id", "")))
    items = []
    for ref in refs:
        evidence = evidence_by_id.get(ref)
        if not evidence:
            continue
        saved = [item for item in variants
                 if (item.get("linkage") or {}).get("source_evidence_id") == ref]
        items.append({
            "evidence_id": ref,
            "speaker": evidence.get("speaker"),
            "timestamp": evidence.get("timestamp"),
            "utterance_id": evidence.get("utterance_id"),
            "source_text": evidence.get("content", ""),
            "selected": ref in selected,
            "selected_variant_id": selected.get(ref),
            "versions": [{
                "variant_id": item.get("object_id"),
                "content": item.get("content", ""),
                "status": item.get("status"),
                "editor": item.get("owner"),
                "created_at": item.get("created_at"),
            } for item in saved],
        })
    return {
        "task_id": task_id, "graph_id": graph_id, "node_id": node_id,
        "editable": graph.get("status") != "approved" and node.get("status") != "approved",
        "selected_count": len(selected), "items": items,
    }


def create_script_variant(task_id, graph_id, node_id, evidence_id, content, editor="admin"):
    if not isinstance(content, str) or not content.strip():
        return {"error": "empty_script_variant"}
    if len(content.encode("utf-8")) > 65536:
        return {"error": "script_variant_too_large", "max_bytes": 65536}
    db = _load_db()
    context, error = _script_graph_context(db, task_id, graph_id, node_id, editable=True)
    if error:
        return error
    node, source = context["node"], context["source"]
    refs = normalize_evidence_refs(db, (node.get("linkage") or {}).get("script_evidence_refs", []))
    evidence = _evidence_for(db, evidence_id)
    if (evidence_id not in refs or not evidence or evidence.get("task_id") != task_id
            or not _is_target_expert_evidence(source, evidence)):
        return {"error": "invalid_script_evidence", "evidence_id": evidence_id}
    for item in db.get("knowledge", {}).values():
        item_linkage = item.get("linkage") or {}
        if (item.get("type") == "script_fragment" and item.get("task_id") == task_id
                and item.get("status") != "archived" and item_linkage.get("editor_variant") is True
                and item_linkage.get("graph_id") == graph_id and item_linkage.get("node_id") == node_id
                and item_linkage.get("source_evidence_id") == evidence_id
                and _normalize_candidate_text(item.get("content")) == _normalize_candidate_text(content)):
            return {"variant": item, "deduplicated": True}
    linkage = {
        "group_id": (node.get("linkage") or {}).get("group_id"),
        "graph_id": graph_id,
        "node_id": node_id,
        "source_evidence_id": evidence_id,
        "script_type": "manual_corrected_verbatim",
        "speaker": evidence.get("speaker"),
        "timestamp": evidence.get("timestamp"),
        "source_utterance_id": evidence.get("utterance_id"),
        "editor_variant": True,
    }
    variant = create_knowledge_object(
        task_id, node.get("source_id"), "script_fragment", content, [evidence_id],
        scope=node.get("scope", "general"), linkage=linkage, owner=editor,
        dedupe_key=_stable_fingerprint({"graph_id": graph_id, "node_id": node_id,
                                        "evidence_id": evidence_id,
                                        "content": _normalize_candidate_text(content)}),
    )
    if "error" in variant:
        return variant
    audit_event(editor, "reviewer", f"task:{task_id}", variant["object_id"],
                "create_script_variant", "saved", f"node={node_id}; evidence={evidence_id}")
    return {"variant": variant, "deduplicated": False}


def save_node_script_selections(task_id, graph_id, node_id, selections, editor="admin"):
    if not isinstance(selections, list):
        return {"error": "invalid_script_selections"}
    db = _load_db()
    context, error = _script_graph_context(db, task_id, graph_id, node_id, editable=True)
    if error:
        return error
    node = context["node"]
    refs = normalize_evidence_refs(db, (node.get("linkage") or {}).get("script_evidence_refs", []))
    requested, seen = {}, set()
    for item in selections:
        if not isinstance(item, dict):
            return {"error": "invalid_script_selection"}
        evidence_id = str(item.get("evidence_id") or "")
        variant_id = item.get("variant_id") or None
        if evidence_id not in refs or evidence_id in seen:
            return {"error": "invalid_script_selection", "evidence_id": evidence_id}
        seen.add(evidence_id)
        if variant_id:
            variant = db.get("knowledge", {}).get(variant_id)
            linkage = (variant or {}).get("linkage") or {}
            if (not variant or variant.get("status") in ("approved", "archived", "rejected")
                    or linkage.get("editor_variant") is not True
                    or linkage.get("graph_id") != graph_id or linkage.get("node_id") != node_id
                    or linkage.get("source_evidence_id") != evidence_id):
                return {"error": "invalid_script_variant", "variant_id": variant_id}
        requested[evidence_id] = variant_id
    canonical = [{"evidence_id": ref, "variant_id": requested[ref]}
                 for ref in refs if ref in requested]
    node.setdefault("linkage", {})["script_selections"] = canonical
    node["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_db(db)
    audit_event(editor, "reviewer", f"task:{task_id}", node_id,
                "save_script_selections", "saved", f"selected={len(canonical)}")
    workspace = get_node_script_workspace(task_id, graph_id, node_id)
    workspace["selected_count"] = len(canonical)
    return workspace


def delete_script_variant(task_id, variant_id, editor="admin"):
    db = _load_db()
    variant = db.get("knowledge", {}).get(variant_id)
    linkage = (variant or {}).get("linkage") or {}
    if (not variant or variant.get("task_id") != task_id or variant.get("type") != "script_fragment"
            or linkage.get("editor_variant") is not True):
        return {"error": "script_variant_not_found"}
    if variant.get("status") == "approved" or variant.get("immutable") is True:
        return {"error": "approved_variant_immutable"}
    if any(variant_id in (item.get("manifest", {}).get("input_objects") or [])
           for item in db.get("compilations", [])):
        return {"error": "script_variant_in_use"}
    node = db.get("knowledge", {}).get(linkage.get("node_id"))
    if node:
        raw = (node.get("linkage") or {}).get("script_selections")
        if isinstance(raw, list):
            for selection in raw:
                if selection.get("variant_id") == variant_id:
                    selection["variant_id"] = None
            node["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    del db["knowledge"][variant_id]
    _save_db(db)
    audit_event(editor, "reviewer", f"task:{task_id}", variant_id,
                "delete_script_variant", "deleted",
                f"node={linkage.get('node_id')}; evidence={linkage.get('source_evidence_id')}")
    result = {"deleted": True, "variant_id": variant_id}
    workspace = get_node_script_workspace(task_id, linkage.get("graph_id"), linkage.get("node_id"))
    if "error" not in workspace:
        result["workspace"] = workspace
    return result


def _node_graph_context(db, task_id, graph_id, node_origin, node_id, editable=False):
    task = _task_for(db, task_id)
    graph = db.get("knowledge", {}).get(graph_id)
    if not task:
        return None, {"error": "task_not_found"}
    if not graph or graph.get("task_id") != task_id or graph.get("type") != "graph":
        return None, {"error": "graph_not_found"}
    if editable and graph.get("status") == "approved":
        return None, {"error": "approved_graph_immutable", "message": "已批准 Graph 的节点只读"}
    source = db.get("sources", {}).get(graph.get("source_id") or task.get("source_id")) or {}
    if node_origin == "candidate":
        node = db.get("knowledge", {}).get(node_id)
        if (not node or node.get("task_id") != task_id or node.get("type") != "strategy_node"
                or node_id not in ((graph.get("linkage") or {}).get("node_ids") or [])):
            return None, {"error": "graph_node_not_found"}
        if editable and (node.get("status") == "approved" or node.get("immutable") is True):
            return None, {"error": "approved_graph_immutable", "message": "已批准节点只读"}
        return {"task": task, "graph": graph, "source": source, "node": node,
                "node_origin": "candidate", "baseline": None, "raw_node": None}, None
    if node_origin != "baseline":
        return None, {"error": "invalid_node_origin", "allowed": ["candidate", "baseline"]}
    baseline, baseline_error = _selected_baseline(db, source, (graph.get("linkage") or {}).get("baseline_id"))
    if baseline_error:
        return None, baseline_error
    raw_node = (_baseline_indexes(baseline)["nodes"]["by_id"].get(str(node_id)) if baseline else None)
    if not raw_node:
        return None, {"error": "graph_node_not_found"}
    return {"task": task, "graph": graph, "source": source, "node": None,
            "node_origin": "baseline", "baseline": baseline, "raw_node": raw_node}, None


def get_node_content_workspace(task_id, graph_id, node_origin, node_id):
    db = _load_db()
    context, error = _node_graph_context(db, task_id, graph_id, node_origin, node_id)
    if error:
        return error
    if node_origin == "candidate":
        node = context["node"]
        linkage = node.get("linkage") or {}
        current = str(node.get("content") or "")
        original = str(linkage.get("extracted_content") if linkage.get("extracted_content") is not None else current)
        evidence_refs = (normalize_evidence_refs(db, node.get("evidence_refs", []))
                         or [str(ref) for ref in (linkage.get("baseline_evidence_refs") or [])])
        context_refs = (normalize_evidence_refs(db, linkage.get("context_refs", []))
                        or [str(ref) for ref in (linkage.get("baseline_context_refs") or [])])
        source_id = node.get("object_id")
        review_status = linkage.get("content_review_status") or "unreviewed"
    else:
        raw = context["raw_node"]
        current = original = str(raw.get("label") or raw.get("content") or "")
        evidence_refs = [str(ref) for ref in (raw.get("evidence_refs") or [])]
        context_refs = [str(ref) for ref in (raw.get("context_refs") or [])]
        source_id = str(node_id)
        review_status = "unreviewed"
    return {
        "task_id": task_id,
        "graph_id": graph_id,
        "node_origin": node_origin,
        "node_id": node_id,
        "source_node_id": source_id,
        "original_content": original,
        "current_content": current,
        "content_review_status": review_status,
        "evidence_refs": evidence_refs,
        "context_refs": context_refs,
        "scripts": list((context.get("raw_node") or {}).get("scripts") or []),
        "expert_utterances": list((context.get("raw_node") or {}).get("expert_utterances") or []),
        "editable": context["graph"].get("status") != "approved",
    }


def save_node_content(task_id, graph_id, node_origin, node_id, content, reviewer="admin"):
    if not reviewer:
        return {"error": "missing_reviewer"}
    if not isinstance(content, str) or not content.strip():
        return {"error": "empty_node_content", "message": "节点文本不能为空"}
    content = content.strip()
    if len(content.encode("utf-8")) > 16384:
        return {"error": "node_content_too_large", "max_bytes": 16384}
    db = _load_db()
    context, error = _node_graph_context(db, task_id, graph_id, node_origin, node_id, editable=True)
    if error:
        return error
    graph = context["graph"]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if node_origin == "candidate":
        node = context["node"]
        linkage = node.setdefault("linkage", {})
        before = str(node.get("content") or "")
        linkage.setdefault("extracted_content", before)
        linkage.update({
            "node_content": content,
            "content_review_status": "confirmed",
            "content_reviewer": reviewer,
            "content_reviewed_at": now,
        })
        node["content"] = content
        node["updated_at"] = now
        node["dedupe_key"] = _candidate_dedupe_key(
            node.get("type"), content, node.get("evidence_refs", []), linkage
        )
        target_node_id = node_id
        _save_db(db)
    else:
        baseline, raw = context["baseline"], context["raw_node"]
        for candidate_id in ((graph.get("linkage") or {}).get("node_ids") or []):
            candidate = db.get("knowledge", {}).get(candidate_id)
            if (candidate and candidate.get("type") == "strategy_node"
                    and str(node_id) in [str(ref) for ref in ((candidate.get("linkage") or {}).get("baseline_refs") or [])]):
                return save_node_content(task_id, graph_id, "candidate", candidate_id, content, reviewer)
        original = str(raw.get("label") or raw.get("content") or "")
        linkage = {
            "group_id": (graph.get("linkage") or {}).get("group_id"),
            "candidate_key": _new_id("candidate_node"),
            "change_type": "modify",
            "baseline_id": baseline.get("baseline_id"),
            "baseline_version": baseline.get("version"),
            "baseline_content_hash": baseline.get("content_hash"),
            "baseline_refs": [str(node_id)],
            "node_content": content,
            "extracted_content": original,
            "content_review_status": "confirmed",
            "content_reviewer": reviewer,
            "content_reviewed_at": now,
            "change_reason": "人工校准不可变基线节点文本",
            "context_refs": [],
            "baseline_evidence_refs": list(raw.get("evidence_refs") or []),
            "baseline_context_refs": list(raw.get("context_refs") or []),
            "evidence_mode": "baseline_reference",
            "baseline_node_correction": True,
        }
        created = create_knowledge_object(
            task_id, graph.get("source_id"), "strategy_node", content, [],
            scope=graph.get("scope", "general"), linkage=linkage, owner=reviewer,
            dedupe_key=_stable_fingerprint(
                {"graph_id": graph_id, "baseline_node_id": str(node_id), "content": content}
            ),
        )
        if "error" in created:
            return created
        target_node_id = created["object_id"]
        db = _load_db()
        graph = db["knowledge"][graph_id]
        graph.setdefault("linkage", {}).setdefault("node_ids", []).append(target_node_id)
        graph["updated_at"] = now
        _save_db(db)
        before = original
    audit_event(
        reviewer, "reviewer", f"task:{task_id}", target_node_id,
        "confirm_node_content", "saved",
        json.dumps({"graph_id": graph_id, "before": before, "after": content}, ensure_ascii=False),
    )
    return get_node_content_workspace(task_id, graph_id, "candidate", target_node_id)


def _edge_graph_context(db, task_id, graph_id, edge_origin, edge_id, editable=False):
    task = _task_for(db, task_id)
    graph = db.get("knowledge", {}).get(graph_id)
    if not task:
        return None, {"error": "task_not_found"}
    if not graph or graph.get("task_id") != task_id or graph.get("type") != "graph":
        return None, {"error": "graph_not_found"}
    if editable and graph.get("status") == "approved":
        return None, {"error": "approved_graph_immutable", "message": "已批准 Graph 的边条件只读"}
    source = db.get("sources", {}).get(graph.get("source_id") or task.get("source_id")) or {}
    if edge_origin == "candidate":
        edge = db.get("knowledge", {}).get(edge_id)
        if (not edge or edge.get("task_id") != task_id or edge.get("type") != "strategy_edge"
                or edge_id not in ((graph.get("linkage") or {}).get("edge_ids") or [])):
            return None, {"error": "graph_edge_not_found"}
        if editable and (edge.get("status") == "approved" or edge.get("immutable") is True):
            return None, {"error": "approved_graph_immutable", "message": "已批准边条件只读"}
        return {"task": task, "graph": graph, "source": source, "edge": edge,
                "edge_origin": "candidate", "baseline": None, "raw_edge": None}, None
    if edge_origin != "baseline":
        return None, {"error": "invalid_edge_origin", "allowed": ["candidate", "baseline"]}
    baseline, baseline_error = _selected_baseline(db, source, (graph.get("linkage") or {}).get("baseline_id"))
    if baseline_error:
        return None, baseline_error
    raw_edge = (_baseline_indexes(baseline)["edges"]["by_id"].get(str(edge_id)) if baseline else None)
    if not raw_edge:
        return None, {"error": "graph_edge_not_found"}
    return {"task": task, "graph": graph, "source": source, "edge": None,
            "edge_origin": "baseline", "baseline": baseline, "raw_edge": raw_edge}, None


def _graph_change_objects(db, graph):
    linkage = graph.get("linkage") or {}
    ids = [*(linkage.get("node_ids") or []), *(linkage.get("edge_ids") or []),
           *(linkage.get("trigger_ids") or [])]
    return [db.get("knowledge", {}).get(obj_id) for obj_id in ids
            if db.get("knowledge", {}).get(obj_id)]


def get_edge_condition_workspace(task_id, graph_id, edge_origin, edge_id):
    db = _load_db()
    context, error = _edge_graph_context(db, task_id, graph_id, edge_origin, edge_id)
    if error:
        return error
    graph = context["graph"]
    if edge_origin == "candidate":
        edge = context["edge"]
        linkage = edge.get("linkage") or {}
        original = linkage.get("extracted_condition")
        if original is None:
            original = linkage.get("condition") or edge.get("content", "")
        current = linkage.get("condition") or edge.get("content", "")
        refs = (normalize_evidence_refs(db, edge.get("evidence_refs", []))
                or [str(ref) for ref in (linkage.get("baseline_evidence_refs") or [])])
        source_id = edge_id
        review_status = linkage.get("condition_review_status") or (
            "needs_review" if linkage.get("condition_uncertainty") else "unreviewed")
        uncertainty = linkage.get("condition_uncertainty", "")
    else:
        raw = context["raw_edge"]
        original = raw.get("label") or raw.get("condition") or ""
        current = original
        refs = normalize_evidence_refs(db, raw.get("evidence_refs", []))
        source_id = str(edge_id)
        review_status = "unreviewed"
        uncertainty = raw.get("condition_uncertainty", "")
    materialized = materialize_incremental_graph(
        db, graph, _graph_change_objects(db, graph), context.get("baseline") or
        _selected_baseline(db, context["source"], (graph.get("linkage") or {}).get("baseline_id"))[0]
    )
    issues = [item for item in (materialized.get("condition_issues") or [])
              if str(item.get("edge_id")) == source_id]
    return {
        "task_id": task_id,
        "graph_id": graph_id,
        "edge_origin": edge_origin,
        "edge_id": edge_id,
        "source_node_id": str((context.get("raw_edge") or {}).get("source") or
                              (context.get("raw_edge") or {}).get("from_node_id") or
                              ((context.get("edge") or {}).get("linkage") or {}).get("from_node_id") or ""),
        "target_node_id": str((context.get("raw_edge") or {}).get("target") or
                              (context.get("raw_edge") or {}).get("to_node_id") or
                              ((context.get("edge") or {}).get("linkage") or {}).get("to_node_id") or ""),
        "original_condition": str(original or ""),
        "current_condition": str(current or ""),
        "condition_review_status": review_status,
        "condition_uncertainty": str(uncertainty or ""),
        "review_required": bool(issues or review_status == "needs_review"),
        "condition_issues": issues,
        "evidence_refs": refs,
        "editable": graph.get("status") != "approved",
    }


def save_edge_condition(task_id, graph_id, edge_origin, edge_id, condition, reviewer="admin"):
    if not reviewer:
        return {"error": "missing_reviewer"}
    if not isinstance(condition, str) or not condition.strip():
        return {"error": "empty_edge_condition", "message": "空条件不能伪装为已确认"}
    condition = condition.strip()
    if len(condition.encode("utf-8")) > 16384:
        return {"error": "edge_condition_too_large", "max_bytes": 16384}
    db = _load_db()
    context, error = _edge_graph_context(db, task_id, graph_id, edge_origin, edge_id, editable=True)
    if error:
        return error
    graph = context["graph"]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if edge_origin == "candidate":
        edge = context["edge"]
        linkage = edge.setdefault("linkage", {})
        before = str(linkage.get("condition") or edge.get("content", ""))
        linkage.setdefault("extracted_condition", before)
        linkage.update({
            "condition": condition,
            "condition_review_status": "confirmed",
            "condition_reviewer": reviewer,
            "condition_reviewed_at": now,
        })
        edge["content"] = condition
        edge["updated_at"] = now
        edge["dedupe_key"] = _candidate_dedupe_key(
            edge.get("type"), edge.get("content", ""), edge.get("evidence_refs", []), linkage
        )
        target_edge_id = edge_id
        _save_db(db)
    else:
        baseline, raw = context["baseline"], context["raw_edge"]
        existing = None
        for candidate_id in ((graph.get("linkage") or {}).get("edge_ids") or []):
            candidate = db.get("knowledge", {}).get(candidate_id)
            if (candidate and candidate.get("type") == "strategy_edge"
                    and str(edge_id) in [str(ref) for ref in ((candidate.get("linkage") or {}).get("baseline_refs") or [])]):
                existing = candidate
                break
        if existing:
            return save_edge_condition(task_id, graph_id, "candidate", existing["object_id"], condition, reviewer)
        raw_from = str(raw.get("source") or raw.get("from_node_id") or raw.get("from") or "")
        raw_to = str(raw.get("target") or raw.get("to_node_id") or raw.get("to") or "")
        original = str(raw.get("label") or raw.get("condition") or "")
        linkage = {
            "group_id": (graph.get("linkage") or {}).get("group_id"),
            "candidate_key": _new_id("candidate_edge"),
            "change_type": "modify",
            "baseline_id": baseline.get("baseline_id"),
            "baseline_version": baseline.get("version"),
            "baseline_content_hash": baseline.get("content_hash"),
            "baseline_refs": [str(edge_id)],
            "from_ref": raw_from,
            "to_ref": raw_to,
            "from_node_id": raw_from,
            "to_node_id": raw_to,
            "condition": condition,
            "extracted_condition": original,
            "condition_review_status": "confirmed",
            "condition_uncertainty": str(raw.get("condition_uncertainty") or ""),
            "condition_reviewer": reviewer,
            "condition_reviewed_at": now,
            "change_reason": "人工校准不可变基线边的路由条件",
            "context_refs": [],
            "baseline_evidence_refs": list(raw.get("evidence_refs") or []),
            "baseline_context_refs": list(raw.get("context_refs") or []),
            "evidence_mode": "baseline_reference",
            "baseline_condition_correction": True,
        }
        created = create_knowledge_object(
            task_id, graph.get("source_id"), "strategy_edge", condition,
            [], scope=graph.get("scope", "general"), linkage=linkage,
            owner=reviewer, dedupe_key=_stable_fingerprint(
                {"graph_id": graph_id, "baseline_edge_id": str(edge_id), "condition": condition}
            ),
        )
        if "error" in created:
            return created
        target_edge_id = created["object_id"]
        db = _load_db()
        graph = db["knowledge"][graph_id]
        graph.setdefault("linkage", {}).setdefault("edge_ids", []).append(target_edge_id)
        graph["updated_at"] = now
        _save_db(db)
        before = original
    audit_event(
        reviewer, "reviewer", f"task:{task_id}", target_edge_id,
        "confirm_edge_condition", "saved",
        json.dumps({"graph_id": graph_id, "before": before, "after": condition}, ensure_ascii=False),
    )
    return get_edge_condition_workspace(task_id, graph_id, "candidate", target_edge_id)


def review_graph_candidate(task_id, graph_id, reviewer, decision, reason):
    """Review one complete candidate Graph; evidence classification stays backstage."""
    if decision not in ("approved", "rejected"):
        return {"error": "invalid_decision", "allowed": ["approved", "rejected"]}
    if not reviewer:
        return {"error": "missing_reviewer"}
    db = _load_db()
    task = _task_for(db, task_id)
    graph = db.get("knowledge", {}).get(graph_id)
    if not task:
        return {"error": "task_not_found"}
    if not graph or graph.get("task_id") != task_id or graph.get("type") != "graph":
        return {"error": "graph_not_found"}
    if graph.get("status") not in ("candidate", "pending_review", "rejected"):
        return {"error": "invalid_target_status", "status": graph.get("status")}

    linkage = graph.get("linkage") or {}
    analysis_run = next((run for run in db.get("analysis_runs", [])
                         if run.get("run_id") == graph.get("analysis_run_id")), None)
    rejected_changes = list(linkage.get("rejected_changes") or [])
    if analysis_run and not rejected_changes:
        run_result = analysis_run.get("result") or {}
        for entity_type, key in (("node", "rejected_nodes"), ("edge", "rejected_edges"),
                                 ("trigger", "rejected_triggers")):
            for item in run_result.get(key) or []:
                rejected_changes.append({"entity_type": entity_type, "reason": item.get("reason")})
    if decision == "approved" and rejected_changes:
        return {
            "error": "candidate_change_rejections",
            "message": "候选 Graph 有模型变更未通过服务端校验，修复或重跑前不能批准",
            "rejected_changes": rejected_changes,
        }
    group_id = linkage.get("group_id")
    knowledge = db.get("knowledge", {})
    object_ids = [
        *(linkage.get("node_ids") or []),
        *(linkage.get("edge_ids") or []),
        *(linkage.get("trigger_ids") or []),
        graph_id,
    ]
    if group_id:
        object_ids.append(group_id)
    objects = [knowledge[obj_id] for obj_id in dict.fromkeys(object_ids) if obj_id in knowledge]
    source = db.get("sources", {}).get(graph.get("source_id")) or {}
    if decision == "approved":
        baseline, baseline_error = _selected_baseline(db, source, linkage.get("baseline_id"))
        if baseline_error:
            return baseline_error
        materialized = materialize_incremental_graph(db, graph, objects, baseline)
        condition_issues = materialized.get("condition_issues") or []
        if condition_issues:
            return {
                "error": "missing_branch_conditions",
                "message": "完整 Graph 存在未确认、空白或互相冲突的问题边，处理前不能批准",
                "condition_issues": condition_issues,
            }
        script_issues = materialized.get("script_selection_issues") or []
        if script_issues:
            return {
                "error": "invalid_script_selections",
                "message": "节点原话选择包含无效证据或版本，修复前不能批准",
                "issues": script_issues,
            }

    direct_kinds = {}
    context_refs = set()
    for obj in objects:
        if obj.get("type") in ("strategy_node", "strategy_edge", "strategy_trigger"):
            obj_linkage = obj.get("linkage") or {}
            baseline_correction = (
                not obj.get("evidence_refs") and (
                    (obj.get("type") == "strategy_edge"
                     and _valid_baseline_condition_correction(db, task_id, obj_linkage))
                    or (obj.get("type") == "strategy_node"
                        and _valid_baseline_node_correction(db, task_id, obj_linkage))
                )
            )
            if not baseline_correction:
                attribution_error = _expert_support_error(
                    db, source, obj.get("evidence_refs", []), obj_linkage.get("context_refs", [])
                )
                if attribution_error:
                    return {"error": attribution_error["error"], "object_id": obj.get("object_id")}
        for ref in normalize_evidence_refs(db, obj.get("evidence_refs", [])):
            direct_kinds.setdefault(ref, "strategy")
        obj_linkage = obj.get("linkage") or {}
        if obj.get("type") == "strategy_node":
            selected_examples, selection_issues = _resolve_node_script_examples(db, obj, graph_id)
            if selection_issues:
                return {"error": "invalid_script_selections", "object_id": obj.get("object_id"),
                        "issues": selection_issues}
            for example in selected_examples:
                direct_kinds[example["evidence_id"]] = "script"
        context_refs.update(normalize_evidence_refs(db, obj_linkage.get("context_refs", [])))

    all_evidence_refs = set(direct_kinds) | context_refs
    invalid_evidence = []
    for ref in all_evidence_refs:
        item = _evidence_for(db, ref)
        if not item or item.get("task_id") != task_id:
            invalid_evidence.append(ref)
    if invalid_evidence:
        return {"error": "unknown_evidence_ref", "evidence_refs": sorted(invalid_evidence)}

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    target_status = "approved" if decision == "approved" else "rejected"
    if decision == "approved":
        for node in [item for item in objects if item.get("type") == "strategy_node"]:
            node_linkage = node.get("linkage") or {}
            selected_examples, selection_issues = _resolve_node_script_examples(db, node, graph_id)
            if selection_issues:
                return {"error": "invalid_script_selections", "object_id": node.get("object_id"),
                        "issues": selection_issues}
            for example in selected_examples:
                ref = example["evidence_id"]
                if example.get("version_id"):
                    selected_variant = knowledge.get(example["version_id"])
                    selected_variant.setdefault("linkage", {})["group_id"] = group_id
                    selected_variant["linkage"]["selected_for_graph_id"] = graph_id
                    if selected_variant not in objects:
                        objects.append(selected_variant)
                    continue
                existing = next((item for item in knowledge.values()
                                 if item.get("type") == "script_fragment"
                                 and item.get("linkage", {}).get("node_id") == node.get("object_id")
                                 and item.get("linkage", {}).get("editor_variant") is not True
                                 and ref in (item.get("evidence_refs") or [])
                                 and item.get("status") != "archived"), None)
                if existing:
                    if existing not in objects:
                        objects.append(existing)
                    continue
                evidence = _evidence_for(db, ref) or {}
                script_id = _new_id("ko")
                script_linkage = {
                    "group_id": group_id,
                    "node_id": node.get("object_id"),
                    "script_type": "expert_verbatim",
                    "speaker": evidence.get("speaker"),
                    "timestamp": evidence.get("timestamp"),
                    "source_utterance_id": evidence.get("utterance_id"),
                    "source_evidence_id": ref,
                    "selected_for_graph_id": graph_id,
                    "editor_variant": False,
                }
                script = {
                    "object_id": script_id, "type": "script_fragment", "version": 1,
                    "status": "approved", "scope": node.get("scope", "general"),
                    "parent_version": None, "supersedes": None,
                    "content": evidence.get("content", ""), "evidence_refs": [ref],
                    "conflict_set": None, "linkage": script_linkage,
                    "owner": reviewer, "task_id": task_id, "source_id": node.get("source_id"),
                    "analysis_run_id": node.get("analysis_run_id"),
                    "dedupe_key": _candidate_dedupe_key("script_fragment", evidence.get("content", ""), [ref], script_linkage),
                    "duplicate_of": None, "possible_duplicate_of": None,
                    "created_at": now, "updated_at": now, "immutable": True,
                }
                knowledge[script_id] = script
                objects.append(script)
        for item in db.get("evidence", []):
            evidence_id = item.get("evidence_id")
            if evidence_id not in all_evidence_refs:
                continue
            item["evidence_kind"] = direct_kinds.get(evidence_id, "context")
            item["status"] = "approved"
            item["reviewed_by"] = reviewer
            item["reviewed_at"] = now
            item["review_reason"] = "由 Graph 结构及原话证据审核反向确认"
        for obj in objects:
            obj["status"] = "approved"
            obj["immutable"] = True
            obj["updated_at"] = now
        needs_g4 = any(item.get("task_id") == task_id
                       and item.get("type") in ("policy_guard", "scoring_rule")
                       and item.get("status") != "archived"
                       for item in knowledge.values())
        task["current_gate"] = "G4" if needs_g4 else "G5"
        task["last_completed_gate"] = "G3"
    else:
        for obj in objects:
            if obj.get("status") != "approved":
                obj["status"] = target_status
                obj["updated_at"] = now

    gate_refs = sorted(all_evidence_refs)
    if decision == "approved":
        db.setdefault("gates", []).append({
            "audit_id": _new_id("aud"), "gate_id": "G2", "task_id": task_id,
            "reviewer": reviewer, "reviewer_role": GATE_DEFS["G2"]["name"],
            "decision": "approved", "reason": "Graph 审核反向确认其引用证据",
            "target_object_id": graph_id, "evidence_refs": gate_refs,
            "created_at": now, "immutable": True, "review_surface": "graph",
        })
    gate_record = {
        "audit_id": _new_id("aud"), "gate_id": "G3", "task_id": task_id,
        "reviewer": reviewer, "reviewer_role": GATE_DEFS["G3"]["name"],
        "decision": decision, "reason": reason, "target_object_id": graph_id,
        "evidence_refs": gate_refs, "created_at": now, "immutable": True,
        "review_surface": "graph", "reviewed_object_ids": [obj.get("object_id") for obj in objects],
    }
    db.setdefault("gates", []).append(gate_record)
    task["updated_at"] = now
    db.setdefault("access_audit", []).append({
        "audit_id": gate_record["audit_id"], "actor": reviewer, "role": "reviewer",
        "scope": "graph_candidate", "object_id": graph_id, "action": "review_complete_graph",
        "data_level": "D2", "result": decision, "reason": reason,
        "timestamp": now, "immutable": True,
    })
    _save_db(db)
    return {
        "graph_id": graph_id,
        "group_id": group_id,
        "decision": decision,
        "reviewed_object_ids": [obj.get("object_id") for obj in objects],
        "evidence_refs": gate_refs,
        "audit_id": gate_record["audit_id"],
        "next_gate": task.get("current_gate"),
    }


# ── D-06: 权限与不可变审计 ────────────────────────
ROLES = {
    "operator": {"name": "产品/运营操作人", "d_level": "D2"},
    "reviewer": {"name": "知识审核人", "d_level": "D2"},
    "policy_owner": {"name": "政策责任人", "d_level": "D2"},
    "release_owner": {"name": "发布责任人", "d_level": "D1"},
}


def check_permission(role, action, data_level="D2"):
    """检查角色是否有权限执行操作"""
    if role not in ROLES:
        return False, "unknown_role"

    role_d = ROLES[role]["d_level"]
    # D1 < D2 < D3
    levels = {"D1": 1, "D2": 2, "D3": 3}
    if levels.get(role_d, 0) < levels.get(data_level, 3):
        return False, "insufficient_level"

    # D3 操作需要额外审计
    if data_level == "D3" and action in ("view", "export", "download"):
        return True, "requires_audit"

    return True, "allowed"


def audit_event(actor, role, scope, obj_id, action, result, reason="", data_level="D2"):
    """记录不可变审计事件"""
    db = _load_db()
    if "access_audit" not in db:
        db["access_audit"] = []

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    audit_id = _new_id("aud")

    event = {
        "audit_id": audit_id,
        "actor": actor,
        "role": role,
        "scope": scope,
        "object_id": obj_id,
        "action": action,
        "data_level": data_level,
        "result": result,
        "reason": reason,
        "timestamp": now,
        "immutable": True,
    }

    db["access_audit"].append(event)
    _save_db(db)
    return event


def list_audit_events(actor=None, action=None, obj_id=None):
    """查询审计事件"""
    db = _load_db()
    result = db.get("access_audit", [])
    if actor:
        result = [e for e in result if e.get("actor") == actor]
    if action:
        result = [e for e in result if e.get("action") == action]
    if obj_id:
        result = [e for e in result if e.get("object_id") == obj_id]
    return result


# ── 查询接口 ─────────────────────────────────────
def list_evidence(task_id=None, source_id=None):
    db = _load_db()
    result = db["evidence"]
    if task_id:
        result = [e for e in result if e.get("task_id") == task_id]
    if source_id:
        result = [e for e in result if e.get("source_id") == source_id]
    return result


def list_tasks():
    db = _load_db()
    return db["tasks"]


def list_sources(task_id=None):
    db = _load_db()
    if task_id:
        return [s for s in db["sources"].values() if s.get("task_id") == task_id]
    return list(db["sources"].values())


def list_sources_safe(task_id=None):
    """Return source metadata without exposing the immutable D3 snapshot."""
    result = []
    for source in list_sources(task_id):
        safe = dict(source)
        safe["snapshot_available"] = bool(safe.pop("snapshot", None))
        safe["snapshot_redacted"] = True
        result.append(safe)
    return result


def review_evidence(task_id, evidence_id, reviewer, decision,
                    evidence_kind=None, conflict_set=None, reason=""):
    """Review evidence classification without changing source content."""
    db = _load_db()
    if not reviewer:
        return {"error": "missing_reviewer"}
    if decision not in ("accept", "reclassify", "pending", "conflict"):
        return {"error": "invalid_decision",
                "allowed": ["accept", "reclassify", "pending", "conflict"]}
    if not approved_gate(db, task_id, "G1"):
        return {"error": "gate_required", "gate_id": "G1"}
    evidence = _evidence_for(db, evidence_id)
    if not evidence or evidence.get("task_id") != task_id:
        return {"error": "evidence_not_found"}
    if decision == "reclassify" and evidence_kind not in ("strategy", "script", "context", "meta"):
        return {"error": "invalid_evidence_kind"}
    if decision == "conflict" and not conflict_set:
        return {"error": "missing_conflict_set"}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    before_kind = evidence.get("evidence_kind")
    if decision == "reclassify":
        evidence["evidence_kind"] = evidence_kind
    evidence["status"] = "approved" if decision in ("accept", "reclassify") else "candidate"
    evidence["conflict_set"] = conflict_set if decision == "conflict" else None
    evidence["review_decision"] = decision
    evidence["review_reason"] = reason
    evidence["reviewed_by"] = reviewer
    evidence["reviewed_at"] = now
    _save_db(db)
    audit = audit_event(
        actor=reviewer, role="reviewer", scope="evidence",
        obj_id=evidence_id, action="review_evidence", result=decision,
        reason=f"{before_kind} -> {evidence.get('evidence_kind')}; {reason}",
        data_level="D2",
    )
    result = dict(evidence)
    result["audit_id"] = audit["audit_id"]
    return result


def list_utterances(source_id=None):
    db = _load_db()
    result = db["utterances"]
    if source_id:
        result = [u for u in result if u.get("source_id") == source_id]
    return result


def list_sessions(source_id=None):
    db = _load_db()
    result = list(db["sessions"].values())
    if source_id:
        result = [s for s in result if s.get("source_id") == source_id]
    return result


def list_input_files():
    """列出输入用猎头访谈文档文件夹中的可用 TXT"""
    result = []
    if os.path.isdir(INPUT_DIR):
        for f in sorted(os.listdir(INPUT_DIR)):
            if f.endswith(".txt"):
                fp = os.path.join(INPUT_DIR, f)
                size = os.path.getsize(fp)
                result.append({"filename": f, "size": size})
    return result


def get_source_snapshot(source_id):
    db = _load_db()
    source = db["sources"].get(source_id)
    if not source:
        return None
    return source


# ── D-02: LLM 辅助证据提炼 ────────────────────────
def llm_extract_evidence(source_id, max_utts=20):
    """用 LLM 阅读访谈发言，提议 evidence_kind 分类和候选片段"""
    db = _load_db()
    source = db["sources"].get(source_id)
    if not source:
        return {"error": "source_not_found"}
    gate_error = extraction_gate_error(source)
    if gate_error:
        return gate_error

    utterances = [u for u in db["utterances"] if u.get("source_id") == source_id]
    if not utterances:
        return {"error": "no_utterances", "message": "source has no parsed utterances"}

    requested_max_utts = max_utts
    max_utts = _effective_max_utts(max_utts)
    utts_to_send = utterances[:max_utts]
    run_options = {
        "requested_max_utts": requested_max_utts,
        "effective_max_utts": max_utts,
        "utterance_refs": [u.get("utterance_id") for u in utts_to_send],
        "prompt_version": EVIDENCE_PROMPT_VERSION,
        "schema_version": ANALYSIS_SCHEMA_VERSION,
    }
    input_fingerprint = _analysis_input_fingerprint(
        source, "evidence_classification", run_options=run_options
    )
    previous_run = _find_completed_analysis_run(source_id, "evidence_classification", input_fingerprint)
    if previous_run:
        return _reuse_analysis_run(previous_run)

    # 构建 LLM prompt
    lines = []
    for u in utts_to_send:
        lines.append("[{}] {}: {}".format(u["utterance_id"], u["speaker"], u["content"][:200]))
    
    transcript_text = "\n".join(lines)

    expert_name = source.get("target_expert", "")
    system_prompt = (
        "你是一个猎头策略分析助手。目标专家是：" + expert_name + "。下面是一份猎头访谈记录的发言列表。"
        "请逐条分析每条发言，为每条发言给出以下分类之一：\n"
        "- strategy: 目标专家（" + expert_name + "）亲口描述了电话策略、动作、流程或判断标准\n"
        "- script: 目标专家（" + expert_name + "）亲口说了可直接用于电话的原话话术\n"
        "- context: 上下文、提问、引子，或非目标专家（非" + expert_name + "）的发言——这些仅作为理解目标专家发言的背景，不提炼为策略\n"
        "- meta: 会议元信息、寒暄或无关内容\n"
        "重要：只有目标专家（" + expert_name + "）亲口说的内容才能标为 strategy 或 script。"
        "其他人（访谈者、同事等）的策略建议、观点或话术，即使被目标专家以'对''嗯''是'等方式肯定，也标为 context，"
        "并在 reason 中注明'目标专家对某某的肯定表态'。不要把别人的策略当成目标专家的策略。"
        "请以 JSON 数组返回，每项包含 utterance_id、evidence_kind、reason（一句话理由）。"
        "只返回 JSON，不要其他文字。"
    )

    user_prompt = "发言列表：\n" + transcript_text
    if not llm_client:
        return {"error": "llm_client_not_available"}

    result = llm_client.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], max_tokens=65536)

    if "error" in result:
        return {"error": "llm_call_failed", "detail": result}

    # 解析 LLM 返回的 JSON
    raw_content = result.get("content", "")
    # 尝试提取 JSON 数组
    import json as _json
    classifications = []
    try:
        # 找到第一个 [ 和最后一个 ]
        start = raw_content.find("[")
        end = raw_content.rfind("]")
        if start >= 0 and end > start:
            json_str = raw_content[start:end + 1]
            classifications = _json.loads(json_str)
        else:
            return {"error": "llm_parse_failed", "raw": raw_content[:500]}
    except _json.JSONDecodeError as e:
        return {"error": "llm_json_error", "message": str(e), "raw": raw_content[:500]}

    # 将 LLM 分类结果写回证据
    run = _create_analysis_run(source, "evidence_classification", input_fingerprint, run_options)
    run_id = run["run_id"]
    db = _load_db()
    updated = 0
    enforced_classifications = []
    for cls in classifications:
        uid = cls.get("utterance_id", "")
        kind = cls.get("evidence_kind", "context")
        reason = cls.get("reason", "")
        for ev in db["evidence"]:
            if ev.get("utterance_id") == uid and ev.get("source_id") == source_id:
                if kind not in VALID_EVIDENCE_KINDS:
                    kind, reason = "context", "LLM 返回了非法分类，服务端降级为 context"
                if kind in ("strategy", "script") and not _is_target_expert_evidence(source, ev):
                    kind, reason = "context", "非目标专家发言仅作为上下文"
                candidate = {
                    "version": len(ev.get("classification_candidates", [])) + 1,
                    "evidence_kind": kind,
                    "reason": reason,
                    "analysis_run_id": run_id,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                ev.setdefault("classification_candidates", []).append(candidate)
                if ev.get("status") == "approved":
                    enforced_classifications.append(dict(cls, evidence_kind=kind, reason=reason, preserved_approved=True))
                    break
                ev["evidence_kind"] = kind
                ev["llm_classification_reason"] = reason
                ev["status"] = "candidate"  # 保持 candidate，不自动批准
                ev["analysis_run_id"] = run_id
                updated += 1
                enforced_classifications.append(dict(cls, evidence_kind=kind, reason=reason))
                break

    response = {
        "source_id": source_id,
        "total_utterances": len(utterances),
        "sent_to_llm": len(utts_to_send),
        "classified": len(classifications),
        "updated_evidence": updated,
        "classifications": enforced_classifications,
        "requested_max_utts": requested_max_utts,
        "effective_max_utts": max_utts,
        "llm_model": result.get("model", ""),
        "llm_usage": result.get("usage", {}),
        "run_id": run_id,
        "deduplicated": False,
    }
    _save_db(db)
    _finish_analysis_run(run_id, response)
    return response
# ── D-06: LLM 话术文档导入与基线节点映射 ──────────
def llm_map_script_documents(task_id, document_text, document_filename="", baseline_id=None):
    """用 LLM 读取话术文档，将话术映射到基线 Graph 的节点上。"""
    db = _load_db()
    task = _task_for(db, task_id)
    if not task:
        return {"error": "task_not_found"}
    source = db.get("sources", {}).get(task.get("source_id"), {"task_id": task_id})
    baseline, baseline_error = _selected_baseline(db, source, baseline_id)
    if baseline_error:
        return baseline_error
    if not baseline:
        return {"error": "no_baseline", "message": "请先导入基线 Graph 再导入话术文档"}
    bg = baseline.get("graph", {})
    b_nodes = bg.get("nodes", [])
    if not b_nodes:
        return {"error": "no_baseline_nodes", "message": "基线 Graph 没有节点"}

    node_lines = []
    for n in b_nodes:
        node_lines.append("[{}] {}".format(n.get("id", ""), n.get("label", "")))
    node_list_text = "\n".join(node_lines)

    system_prompt = """你是一个话术映射助手。下面有一份话术文档和一组策略节点列表。
请将文档中的话术内容映射到最相关的策略节点上。每条话术标注：
- node_id: 对应的基线节点 ID
- text: 话术原文
- script_type: direct_script（可直接用于电话的原话）/ partial_script（片段或承接词）/ strategy_only（动作逻辑但无可独立使用的句子）
- reason: 简短理由
如果某段话术无法映射到任何节点，node_id 设为 null。
以 JSON 数组返回：[{"node_id":"","text":"","script_type":"","reason":""}]
只返回 JSON，不要其他文字。"""

    user_content = "策略节点列表：\n" + node_list_text + "\n\n话术文档内容：\n" + document_text[:8000]

    if not llm_client:
        return {"error": "llm_client_not_available"}

    result = llm_client.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ], max_tokens=65536)

    if "error" in result:
        return {"error": "llm_call_failed", "detail": result}

    raw_content = result.get("content", "")
    import json as _json
    mappings = None
    try:
        start = raw_content.find("[")
        end = raw_content.rfind("]")
        if start >= 0 and end > start:
            mappings = _json.loads(raw_content[start:end + 1])
        else:
            return {"error": "llm_parse_failed", "raw": raw_content[:500]}
    except _json.JSONDecodeError as e:
        return {"error": "llm_json_error", "message": str(e), "raw": raw_content[:500]}

    scripts_by_node = {}
    unmapped = []
    for m in mappings:
        if not isinstance(m, dict):
            continue
        nid = m.get("node_id")
        script_entry = {
            "text": m.get("text", ""),
            "script_type": m.get("script_type", "unknown"),
            "reason": m.get("reason", ""),
            "source": document_filename,
        }
        if nid and any(node.get("id") == nid for node in b_nodes):
            scripts_by_node.setdefault(nid, []).append(script_entry)
        else:
            if nid:
                script_entry["reason"] = "LLM 返回了不属于所选基线的 node_id"
            unmapped.append(script_entry)

    candidate = {
        "candidate_id": _new_id("scriptdoc"),
        "task_id": task_id,
        "source_id": task.get("source_id"),
        "baseline_id": baseline.get("baseline_id"),
        "baseline_version": baseline.get("version"),
        "baseline_content_hash": baseline.get("content_hash"),
        "document_filename": document_filename,
        "document_hash": _sha256_bytes(document_text.encode("utf-8")),
        "mappings": mappings,
        "scripts_by_node": scripts_by_node,
        "unmapped_scripts": unmapped,
        "status": "candidate",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "immutable_baseline": True,
    }
    db.setdefault("script_document_candidates", []).append(candidate)
    _save_db(db)

    response = {
        "task_id": task_id,
        "baseline_id": baseline.get("baseline_id"),
        "baseline_version": baseline.get("version"),
        "baseline_content_hash": baseline.get("content_hash"),
        "candidate_id": candidate["candidate_id"],
        "document_filename": document_filename,
        "total_mappings": len(mappings),
        "candidate_nodes": len(scripts_by_node),
        "updated_nodes": 0,
        "unmapped_scripts": unmapped,
        "mappings": mappings,
        "llm_model": result.get("model", ""),
        "llm_usage": result.get("usage", {}),
    }
    return response


# ── C-01: 知识对象最小 schema ──────────────────────
# 对象类型: strategy_node / script_fragment / policy_guard / scoring_rule
# 状态: candidate / pending_review / approved / rejected / archived
# 结构: 稳定ID、版本、父版本(supersedes)、作用域、证据refs、冲突集、linkage

OBJECT_TYPES = [
    "expert", "strategy_script_group", "graph", "fragment", "overlay",
    "strategy_node", "strategy_edge", "strategy_trigger", "candidate_state",
    "script", "script_fragment", "policy_guard", "scoring_rule",
]
OBJECT_STATUS = ["candidate", "pending_review", "approved", "rejected", "archived"]
CHANGE_TYPES = ["add", "modify", "reclassify", "link", "attach",
                "replace", "deprecate", "split", "merge", "unlink", "detach",
                "override", "overlay"]
STRUCTURAL_CHANGES = ["replace", "deprecate", "split", "merge", "unlink", "detach", "override", "overlay"]


def normalize_evidence_refs(db, evidence_refs):
    """Resolve legacy utterance refs to evidence IDs without mutating old objects."""
    by_utterance = {
        item.get("utterance_id"): item.get("evidence_id")
        for item in db.get("evidence", [])
        if item.get("utterance_id") and item.get("evidence_id")
    }
    return [by_utterance.get(ref, ref) for ref in (evidence_refs or [])]


def validate_evidence_refs(db, evidence_refs):
    """Only allow refs that resolve to the immutable evidence layer."""
    known = {item.get("evidence_id") for item in db.get("evidence", [])}
    unknown = [ref for ref in (evidence_refs or []) if ref not in known]
    return unknown


def validate_knowledge_linkage(db, obj_type, linkage, task_id=None):
    """Validate the minimum relations needed by first-class graph objects."""
    linkage = linkage or {}
    baseline = None
    baseline_node_ids = set()
    if linkage.get("baseline_id"):
        baseline = next((item for item in db.get("graph_baselines", [])
                         if item.get("baseline_id") == linkage.get("baseline_id")
                         and (not task_id or item.get("task_id") == task_id)), None)
        if not baseline:
            return {"error": "baseline_not_found", "baseline_id": linkage.get("baseline_id")}
        actual_hash = _stable_fingerprint(baseline.get("graph") or {})
        if (linkage.get("baseline_version") is not None
                and linkage.get("baseline_version") != baseline.get("version")):
            return {"error": "baseline_version_mismatch", "baseline_id": linkage.get("baseline_id")}
        if actual_hash != baseline.get("content_hash") or (
                linkage.get("baseline_content_hash")
                and linkage.get("baseline_content_hash") != actual_hash):
            return {"error": "baseline_hash_mismatch", "baseline_id": linkage.get("baseline_id")}
        baseline_node_ids = {
            str(item.get("id")) for item in (baseline.get("graph") or {}).get("nodes", [])
            if item.get("id") is not None
        }
    if obj_type in ("graph", "fragment") and not linkage.get("group_id"):
        return {"error": "missing_group_id", "message": f"{obj_type} must belong to a strategy_script_group"}
    if obj_type == "overlay":
        if not linkage.get("group_id") or not linkage.get("base_ref"):
            return {"error": "missing_overlay_base", "message": "overlay requires group_id and base_ref"}
        base = db.get("knowledge", {}).get(linkage["base_ref"])
        if not base or base.get("status") != "approved":
            return {"error": "invalid_overlay_base", "message": "overlay base_ref must point to an approved object"}
        if base.get("linkage", {}).get("group_id") != linkage.get("group_id"):
            return {"error": "cross_group_overlay", "message": "overlay base_ref must be in the same group"}
    if obj_type == "strategy_edge":
        if not linkage.get("from_node_id") or not linkage.get("to_node_id") or not linkage.get("condition"):
            return {"error": "missing_edge_relation", "message": "strategy_edge requires from/to node and condition"}
        for node_id in (linkage.get("from_node_id"), linkage.get("to_node_id")):
            node = db.get("knowledge", {}).get(node_id)
            if not node and str(node_id) in baseline_node_ids:
                continue
            if not node or node.get("type") != "strategy_node":
                return {"error": "invalid_edge_node", "object_id": node_id}
            if linkage.get("group_id") and node.get("linkage", {}).get("group_id") != linkage.get("group_id"):
                return {"error": "cross_group_edge"}
    if obj_type == "strategy_trigger":
        if not linkage.get("condition") or not linkage.get("target_node_id"):
            return {"error": "missing_trigger_relation", "message": "strategy_trigger requires condition and target node"}
        node = db.get("knowledge", {}).get(linkage.get("target_node_id"))
        if not node and str(linkage.get("target_node_id")) in baseline_node_ids:
            node = None
        elif not node or node.get("type") != "strategy_node":
            return {"error": "invalid_trigger_node", "object_id": linkage.get("target_node_id")}
        if node and linkage.get("group_id") and node.get("linkage", {}).get("group_id") != linkage.get("group_id"):
            return {"error": "cross_group_trigger"}
    if obj_type in ("script", "script_fragment") and not linkage.get("node_id"):
        return {"error": "missing_script_node", "message": "script must link to a strategy node"}
    return None


def _candidate_dedupe_key(obj_type, content, evidence_refs, linkage=None):
    refs = sorted(set(evidence_refs or []))
    linkage = linkage or {}
    if obj_type in ("strategy_node", "graph", "fragment") and refs:
        basis = {"type": obj_type, "evidence_refs": refs,
                 "content": _normalize_candidate_text(content)}
    elif obj_type in ("script", "script_fragment") and refs:
        # Script wording is meaningful: the same evidence can legitimately
        # produce alternative phrasings. Only identical normalized wording
        # is an exact duplicate; shared evidence with different wording is
        # surfaced through possible_duplicate for human comparison.
        basis = {"type": obj_type, "evidence_refs": refs,
                 "content": _normalize_candidate_text(content),
                 "script_type": linkage.get("script_type", "")}
    elif obj_type in ("strategy_edge", "strategy_trigger"):
        basis = {"type": obj_type, "evidence_refs": refs,
                 "content": _normalize_candidate_text(content),
                 "condition": _normalize_candidate_text(linkage.get("condition", ""))}
    else:
        basis = {"type": obj_type, "content": _normalize_candidate_text(content),
                 "evidence_refs": refs}
    return _stable_fingerprint(basis)


def _find_duplicate_candidate(db, task_id, source_id, obj_type, dedupe_key):
    if not dedupe_key:
        return None
    for obj in db.get("knowledge", {}).values():
        existing_key = obj.get("dedupe_key") or _candidate_dedupe_key(
            obj.get("type"), obj.get("content", ""), obj.get("evidence_refs", []), obj.get("linkage")
        )
        if (obj.get("task_id") == task_id and obj.get("source_id") == source_id
                and obj.get("type") == obj_type
                and existing_key == dedupe_key
                and obj.get("status") != "rejected"):
            return obj
    return None


def _find_possible_duplicate(db, task_id, source_id, obj_type, evidence_refs, dedupe_key):
    refs = sorted(set(evidence_refs or []))
    if not refs:
        return None
    for obj in db.get("knowledge", {}).values():
        if (obj.get("task_id") != task_id or obj.get("source_id") != source_id
                or obj.get("type") != obj_type or obj.get("status") in ("rejected", "archived")):
            continue
        if sorted(set(obj.get("evidence_refs") or [])) == refs:
            existing_key = obj.get("dedupe_key") or _candidate_dedupe_key(
                obj.get("type"), obj.get("content", ""), obj.get("evidence_refs", []), obj.get("linkage")
            )
            if existing_key != dedupe_key:
                return obj
    return None


def create_knowledge_object(task_id, source_id, obj_type, content, evidence_refs,
                             scope="general", parent_version=None, linkage=None,
                             conflict_set=None, owner="admin", analysis_run_id=None,
                             dedupe_key=None):
    """C-01: 创建候选知识对象"""
    db = _load_db()
    if "knowledge" not in db:
        db["knowledge"] = {}
    if "changes" not in db:
        db["changes"] = []

    if obj_type not in OBJECT_TYPES:
        return {"error": "invalid_object_type", "allowed": OBJECT_TYPES}
    normalized_refs = normalize_evidence_refs(db, evidence_refs)
    unknown_refs = validate_evidence_refs(db, normalized_refs)
    if unknown_refs:
        return {"error": "unknown_evidence_ref", "evidence_refs": unknown_refs}
    source = db.get("sources", {}).get(source_id)
    if source and obj_type in ("strategy_node", "strategy_edge", "strategy_trigger",
                               "candidate_state", "script", "script_fragment"):
        baseline_correction = (not normalized_refs and (
            (obj_type == "strategy_edge" and _valid_baseline_condition_correction(db, task_id, linkage))
            or (obj_type == "strategy_node" and _valid_baseline_node_correction(db, task_id, linkage))
        ))
        if not baseline_correction:
            attribution_error = _expert_support_error(
                db, source, normalized_refs, (linkage or {}).get("context_refs", [])
            )
            if attribution_error:
                return attribution_error
    linkage_error = validate_knowledge_linkage(db, obj_type, linkage, task_id)
    if linkage_error:
        return linkage_error

    dedupe_key = dedupe_key or _candidate_dedupe_key(obj_type, content, normalized_refs, linkage)
    duplicate = _find_duplicate_candidate(db, task_id, source_id, obj_type, dedupe_key)
    possible_duplicate = None if duplicate else _find_possible_duplicate(
        db, task_id, source_id, obj_type, normalized_refs, dedupe_key
    )
    obj_id = _new_id("ko")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    obj = {
        "object_id": obj_id,
        "type": obj_type,
        "version": 1,
        "status": "archived" if duplicate else "candidate",
        "scope": scope,
        "parent_version": parent_version,  # supersedes 哪个版本
        "supersedes": None,
        "content": content,
        "evidence_refs": normalized_refs,
        "conflict_set": conflict_set,
        "linkage": linkage or {},  # Graph/Fragment/Overlay 关系
        "owner": owner,
        "task_id": task_id,
        "source_id": source_id,
        "analysis_run_id": analysis_run_id,
        "dedupe_key": dedupe_key,
        "duplicate_of": duplicate.get("object_id") if duplicate else None,
        "possible_duplicate_of": possible_duplicate.get("object_id") if possible_duplicate else None,
        "created_at": now,
        "updated_at": now,
        "immutable": False,  # candidate 可改，approved 后不可原地改
    }

    db["knowledge"][obj_id] = obj
    _save_db(db)
    return obj


def list_knowledge(task_id=None, source_id=None, status=None, obj_type=None, include_archived=False):
    """C-01: 查询知识对象"""
    db = _load_db()
    result = list(db.get("knowledge", {}).values())
    if task_id:
        result = [o for o in result if o.get("task_id") == task_id]
    if source_id:
        result = [o for o in result if o.get("source_id") == source_id]
    if status:
        result = [o for o in result if o.get("status") == status]
    if obj_type:
        result = [o for o in result if o.get("type") == obj_type]
    if not status and not include_archived:
        result = [o for o in result if o.get("status") != "archived"]
        unique = []
        seen = set()
        for obj in result:
            key = obj.get("dedupe_key") or _candidate_dedupe_key(
                obj.get("type"), obj.get("content", ""), obj.get("evidence_refs", []), obj.get("linkage")
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(obj)
        result = unique
    return result


def _normalize_graph_document(document):
    """Normalize the internal graph interchange shape without approving it."""
    if not isinstance(document, dict):
        return {"error": "invalid_graph_document", "message": "graph must be an object"}
    raw_nodes = document.get("nodes") or []
    raw_edges = document.get("edges") or []
    raw_triggers = document.get("triggers") or []
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list) or not isinstance(raw_triggers, list):
        return {"error": "invalid_graph_document", "message": "nodes, edges and triggers must be arrays"}

    nodes = []
    node_ids = set()
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            return {"error": "invalid_graph_node", "index": index}
        for field in ("evidence_refs", "context_refs", "script_ids", "scripts", "expert_utterances"):
            if raw.get(field) is not None and not isinstance(raw.get(field), list):
                return {"error": "invalid_graph_node", "index": index, "field": field}
        if raw.get("metadata") is not None and not isinstance(raw.get("metadata"), dict):
            return {"error": "invalid_graph_node", "index": index, "field": "metadata"}
        node_id = str(raw.get("id") or raw.get("node_id") or f"node_{index + 1}")
        if node_id in node_ids:
            return {"error": "duplicate_graph_node_id", "node_id": node_id}
        node_ids.add(node_id)
        position = raw.get("position") or {}
        nodes.append({
            "id": node_id,
            "label": str(raw.get("label") or raw.get("content") or node_id),
            "description": str(raw.get("description") or ""),
            "kind": str(raw.get("kind") or "action"),
            "status": str(raw.get("status") or "reference"),
            "evidence_refs": sorted(set(str(ref) for ref in (raw.get("evidence_refs") or []) if str(ref))),
            "context_refs": sorted(set(str(ref) for ref in (raw.get("context_refs") or []) if str(ref))),
            "script_ids": list(raw.get("script_ids") or []),
            "position": {
                "x": float(position.get("x", 0) or 0),
                "y": float(position.get("y", 0) or 0),
                "width": float(position.get("width", 160) or 160),
                "height": float(position.get("height", 64) or 64),
            },
            "source_cell_id": raw.get("source_cell_id"),
            "scripts": list(raw.get("scripts") or []),
            "expert_utterances": list(raw.get("expert_utterances") or []),
            "metadata": dict(raw.get("metadata") or {}),
        })

    edges = []
    edge_ids = set()
    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            return {"error": "invalid_graph_edge", "index": index}
        for field in ("evidence_refs", "context_refs"):
            if raw.get(field) is not None and not isinstance(raw.get(field), list):
                return {"error": "invalid_graph_edge", "index": index, "field": field}
        if raw.get("metadata") is not None and not isinstance(raw.get("metadata"), dict):
            return {"error": "invalid_graph_edge", "index": index, "field": "metadata"}
        edge_id = str(raw.get("id") or raw.get("edge_id") or f"edge_{index + 1}")
        if edge_id in edge_ids:
            return {"error": "duplicate_graph_edge_id", "edge_id": edge_id}
        source = str(raw.get("source") or raw.get("from") or "")
        target = str(raw.get("target") or raw.get("to") or "")
        if not source or not target:
            return {"error": "invalid_graph_edge", "edge_id": edge_id, "message": "source and target are required"}
        if source not in node_ids or target not in node_ids:
            return {"error": "dangling_graph_edge", "edge_id": edge_id, "source": source, "target": target}
        edge_ids.add(edge_id)
        edges.append({
            "id": edge_id,
            "source": source,
            "target": target,
            "label": str(raw.get("label") or raw.get("condition") or ""),
            "extracted_condition": str(raw.get("extracted_condition") or raw.get("label") or raw.get("condition") or ""),
            "kind": str(raw.get("kind") or "branch"),
            "status": str(raw.get("status") or "reference"),
            "evidence_refs": sorted(set(str(ref) for ref in (raw.get("evidence_refs") or []) if str(ref))),
            "context_refs": sorted(set(str(ref) for ref in (raw.get("context_refs") or []) if str(ref))),
            "condition_review_status": str(raw.get("condition_review_status") or "unreviewed"),
            "condition_uncertainty": str(raw.get("condition_uncertainty") or ""),
            "condition_reviewer": str(raw.get("condition_reviewer") or ""),
            "condition_reviewed_at": str(raw.get("condition_reviewed_at") or ""),
            "source_cell_id": raw.get("source_cell_id"),
            "metadata": dict(raw.get("metadata") or {}),
        })

    triggers = []
    for index, raw in enumerate(raw_triggers):
        if not isinstance(raw, dict):
            return {"error": "invalid_graph_trigger", "index": index}
        for field in ("evidence_refs", "context_refs"):
            if raw.get(field) is not None and not isinstance(raw.get(field), list):
                return {"error": "invalid_graph_trigger", "index": index, "field": field}
        if raw.get("metadata") is not None and not isinstance(raw.get("metadata"), dict):
            return {"error": "invalid_graph_trigger", "index": index, "field": "metadata"}
        target = str(raw.get("target_node_id") or raw.get("target") or "")
        if target and target not in node_ids:
            return {"error": "dangling_graph_trigger", "target_node_id": target}
        triggers.append({
            "id": str(raw.get("id") or raw.get("trigger_id") or f"trigger_{index + 1}"),
            "target_node_id": target,
            "label": str(raw.get("label") or raw.get("condition") or ""),
            "kind": str(raw.get("kind") or "trigger"),
            "status": str(raw.get("status") or "reference"),
            "evidence_refs": sorted(set(str(ref) for ref in (raw.get("evidence_refs") or []) if str(ref))),
            "context_refs": sorted(set(str(ref) for ref in (raw.get("context_refs") or []) if str(ref))),
            "metadata": dict(raw.get("metadata") or {}),
        })

    if document.get("stop_conditions") is not None and not isinstance(document.get("stop_conditions"), list):
        return {"error": "invalid_graph_document", "field": "stop_conditions"}
    if document.get("metadata") is not None and not isinstance(document.get("metadata"), dict):
        return {"error": "invalid_graph_document", "field": "metadata"}
    return {
        "format": "ai-call-strategy-graph",
        "version": int(document.get("version") or 1),
        "nodes": nodes,
        "edges": edges,
        "triggers": triggers,
        "stop_conditions": list(document.get("stop_conditions") or []),
        "metadata": dict(document.get("metadata") or {}),
    }


def classify_graph_edge_conditions(graph):
    """Return a derived Graph copy with executable edge-condition semantics."""
    graph = graph or {}
    edges = [dict(edge) for edge in graph.get("edges", [])]
    outgoing_counts = {}
    for edge in edges:
        source = str(edge.get("source") or edge.get("from_node_id") or edge.get("from") or "")
        if source:
            outgoing_counts[source] = outgoing_counts.get(source, 0) + 1

    issues = []
    explicit_groups = {}
    for edge in edges:
        source = str(edge.get("source") or edge.get("from_node_id") or edge.get("from") or "")
        condition = str(edge.get("condition") or edge.get("label") or
                        edge.get("extracted_condition") or "").strip()
        if condition:
            edge["condition"] = condition
            edge["condition_kind"] = "explicit"
            edge["condition_display"] = condition
            key = (source, _normalize_candidate_text(condition))
            explicit_groups.setdefault(key, []).append(edge)
            review_status = edge.get("condition_review_status") or (
                "needs_review" if edge.get("condition_uncertainty") else "unreviewed")
            edge["condition_review_status"] = review_status
            if edge.get("condition_uncertainty") and review_status != "confirmed":
                issues.append({
                    "error": "edge_condition_review_required",
                    "edge_id": str(edge.get("id") or edge.get("edge_id") or ""),
                    "source": source,
                    "target": str(edge.get("target") or edge.get("to_node_id") or edge.get("to") or ""),
                    "condition_uncertainty": str(edge.get("condition_uncertainty")),
                })
        elif outgoing_counts.get(source, 0) == 1:
            edge["condition"] = ""
            edge["condition_kind"] = "implicit_sequence"
            edge["condition_display"] = "完成上一步后继续（原图无显式条件）"
        else:
            edge["condition"] = ""
            edge["condition_kind"] = "missing_branch_condition"
            edge["condition_display"] = "⚠ 分支条件缺失（无法判断走向）"
            issues.append({
                "error": "missing_branch_condition",
                "edge_id": str(edge.get("id") or edge.get("edge_id") or ""),
                "source": source,
                "target": str(edge.get("target") or edge.get("to_node_id") or edge.get("to") or ""),
                "source_out_degree": outgoing_counts.get(source, 0),
            })

    for (source, normalized), grouped in explicit_groups.items():
        targets = {str(edge.get("target") or edge.get("to_node_id") or edge.get("to") or "")
                   for edge in grouped}
        if normalized and len(targets) > 1:
            for edge in grouped:
                edge["condition_kind"] = "conflicting_branch_condition"
            issues.append({
                "error": "duplicate_branch_condition",
                "source": source,
                "condition": grouped[0].get("condition", ""),
                "edge_ids": [str(edge.get("id") or edge.get("edge_id") or "") for edge in grouped],
                "targets": sorted(targets),
            })

    classified = dict(graph)
    classified["nodes"] = [dict(node) for node in graph.get("nodes", [])]
    classified["edges"] = edges
    classified["triggers"] = [dict(trigger) for trigger in graph.get("triggers", [])]
    classified["condition_issues"] = issues
    return classified, issues


def _drawio_text(value):
    text = html_lib.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_drawio_document(content, filename=""):
    """Parse uncompressed draw.io XML into the internal reference Graph shape."""
    if not isinstance(content, str) or not content.strip():
        return {"error": "empty_drawio", "message": "draw.io 内容为空"}
    if not str(filename or "").lower().endswith((".drawio", ".drawio.xml", ".xml")):
        return {"error": "unsupported_graph_format", "message": "仅支持 .drawio 或 .drawio.xml"}
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        return {"error": "invalid_drawio_xml", "message": f"draw.io XML 解析失败: {exc}"}
    diagrams = root.findall(".//diagram") if root.tag != "diagram" else [root]
    if not diagrams:
        return {"error": "drawio_diagram_missing", "message": "未找到 diagram 页面"}
    diagram = diagrams[0]
    model = diagram.find(".//mxGraphModel") or (diagram if diagram.tag == "mxGraphModel" else None)
    if model is None:
        return {"error": "drawio_model_missing", "message": "未找到 mxGraphModel，暂不支持压缩页面"}
    cells = model.findall(".//mxCell")
    edge_labels = {
        str(cell.get("parent")): _drawio_text(cell.get("value") or "")
        for cell in cells
        if cell.get("vertex") == "1" and "edgeLabel" in str(cell.get("style") or "") and cell.get("parent")
    }
    nodes = []
    node_ids = set()
    for cell in cells:
        if cell.get("vertex") != "1" or "edgeLabel" in str(cell.get("style") or ""):
            continue
        cell_id = str(cell.get("id") or "")
        if not cell_id or cell_id in ("0", "1") or cell_id in node_ids:
            continue
        geometry = cell.find("./mxGeometry")
        nodes.append({
            "id": cell_id,
            "label": _drawio_text(cell.get("value") or "未命名节点"),
            "kind": "reference",
            "status": "reference",
            "position": {
                "x": float((geometry.get("x") if geometry is not None else 0) or 0),
                "y": float((geometry.get("y") if geometry is not None else 0) or 0),
                "width": float((geometry.get("width") if geometry is not None else 160) or 160),
                "height": float((geometry.get("height") if geometry is not None else 64) or 64),
            },
            "source_cell_id": cell_id,
            "metadata": {"style": cell.get("style", ""), "parent": cell.get("parent")},
        })
        node_ids.add(cell_id)
    edges = []
    for index, cell in enumerate(cells):
        if cell.get("edge") != "1":
            continue
        source, target = cell.get("source"), cell.get("target")
        if source not in node_ids or target not in node_ids:
            continue
        edges.append({
            "id": str(cell.get("id") or f"edge_{index + 1}"),
            "source": source,
            "target": target,
            "label": _drawio_text(cell.get("value") or edge_labels.get(str(cell.get("id"))) or ""),
            "kind": "branch",
            "status": "reference",
            "source_cell_id": cell.get("id"),
            "metadata": {"style": cell.get("style", "")},
        })
    if not nodes:
        return {"error": "drawio_nodes_missing", "message": "未解析到 vertex=1 节点"}
    return _normalize_graph_document({
        "version": 1,
        "nodes": nodes,
        "edges": edges,
        "triggers": [],
        "metadata": {"origin": "drawio", "source_filename": filename, "diagram_count": len(diagrams)},
    })


def parse_graph_import(content, filename=""):
    """Parse the two supported baseline transports into one canonical shape."""
    if str(filename or "").lower().endswith(".json"):
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            return {"error": "invalid_graph_json", "message": str(exc)}
        document = payload.get("graph") if isinstance(payload, dict) and isinstance(payload.get("graph"), dict) else payload
        return _normalize_graph_document(document)
    return parse_drawio_document(content, filename)


def parse_graph_import_bundle(content, filename=""):
    graph = parse_graph_import(content, filename)
    if "error" in graph:
        return graph
    layout_profile = None
    warnings = []
    if str(filename or "").lower().endswith(".json"):
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        candidate = payload.get("layout_profile") if isinstance(payload, dict) else None
        if candidate:
            layout_profile, warnings = _normalize_portable_layout(
                candidate,
                {str(node.get("id")) for node in graph.get("nodes", [])},
                {str(edge.get("id")) for edge in graph.get("edges", [])},
            )
            if layout_profile:
                layout_profile = {
                    "format": "ai-call-graph-layout", "version": 1,
                    "phases": GRAPH_LAYOUT_PHASES, **layout_profile,
                }
                layout_profile["layout_sha256"] = _stable_fingerprint(layout_profile)
    return {"graph": graph, "layout_profile": layout_profile, "warnings": warnings}


def list_graph_baselines(task_id=None, source_id=None):
    db = _load_db()
    result = list(db.get("graph_baselines", []))
    if task_id:
        result = [item for item in result if item.get("task_id") == task_id]
    if source_id:
        result = [item for item in result if item.get("source_id") == source_id]
    enriched = []
    for item in result:
        classified, issues = classify_graph_edge_conditions(item.get("graph") or {})
        enriched.append(dict(item, graph=classified, condition_issues=issues))
    return enriched


def create_graph_baseline(task_id, source_id, name, document, origin="manual", source_filename=None,
                          layout_profile=None):
    db = _load_db()
    task = _task_for(db, task_id)
    if not task:
        return {"error": "task_not_found"}
    source_id = source_id or task.get("source_id")
    if source_id != task.get("source_id"):
        return {"error": "cross_task_source"}
    normalized = _normalize_graph_document(document)
    if "error" in normalized:
        return normalized
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    baseline = {
        "baseline_id": _new_id("base"),
        "task_id": task_id,
        "source_id": source_id,
        "name": str(name or "未命名基线 Graph"),
        "origin": str(origin or "manual"),
        "source_filename": source_filename,
        "format": normalized["format"],
        "version": normalized["version"],
        "graph": normalized,
        "content_hash": _stable_fingerprint(normalized),
        "status": "reference",
        "created_at": now,
        "updated_at": now,
        "immutable": True,
    }
    db.setdefault("graph_baselines", []).append(baseline)
    layout_warnings = []
    if layout_profile:
        portable, layout_warnings = _normalize_portable_layout(
            layout_profile,
            {str(node.get("id")) for node in normalized.get("nodes", [])},
            {str(edge.get("id")) for edge in normalized.get("edges", [])},
        )
        if portable:
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            profile = {
                "layout_id": _new_id("layout"), "task_id": task_id,
                "graph_id": None, "baseline_id": baseline["baseline_id"],
                "materialized_graph_hash": _layout_source_hash(normalized, normalized),
                "status": portable.get("status", "ready"), "auto_nodes": portable["auto_nodes"],
                "auto_edges": portable["auto_edges"],
                "manual_nodes": portable["manual_nodes"],
                "manual_edges": portable["manual_edges"],
                "analysis": {"origin": "portable_import", "prompt_version": None},
                "created_at": now, "updated_at": now,
            }
            profile["layout_sha256"] = _layout_profile_hash(profile)
            db.setdefault("graph_layout_profiles", []).append(profile)
            baseline["layout_id"] = profile["layout_id"]
    if layout_warnings:
        baseline["layout_warnings"] = layout_warnings
    _save_db(db)
    return baseline


def export_graph_document(task_id, graph_id):
    """Export one candidate/approved Graph as the portable baseline interchange shape."""
    db = _load_db()
    graph = db.get("knowledge", {}).get(graph_id)
    if not _task_for(db, task_id):
        return {"error": "task_not_found"}
    if not graph or graph.get("task_id") != task_id or graph.get("type") != "graph":
        return {"error": "graph_not_found"}
    source = db.get("sources", {}).get(graph.get("source_id")) or {}
    baseline, baseline_error = _selected_baseline(
        db, source, (graph.get("linkage") or {}).get("baseline_id")
    )
    if baseline_error:
        return baseline_error
    materialized = materialize_incremental_graph(db, graph, _graph_change_objects(db, graph), baseline)
    portable_input = dict(materialized)
    portable_input["metadata"] = {
        "source_task_id": task_id,
        "source_graph_id": graph_id,
        "source_graph_status": graph.get("status"),
        "source_baseline_id": (baseline or {}).get("baseline_id"),
        "source_baseline_version": (baseline or {}).get("version"),
        "source_baseline_content_hash": (baseline or {}).get("content_hash"),
        "portable_evidence_refs_are_unverified": True,
    }
    normalized = _normalize_graph_document(portable_input)
    if "error" in normalized:
        return normalized
    content_hash = _stable_fingerprint(normalized)
    exported = {
        "format": normalized["format"],
        "version": normalized["version"],
        "graph": normalized,
        "content_hash": content_hash,
        "filename": f"graph_{graph_id}_{content_hash[:12]}.json",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    layout = get_graph_layout(task_id, graph_id)
    if "error" not in layout and layout.get("status") in ("ready", "partial"):
        exported["layout_profile"] = _portable_layout_profile(
            layout,
            {str(node.get("id")) for node in normalized.get("nodes", [])},
            {str(edge.get("id")) for edge in normalized.get("edges", [])},
        )
        exported["layout_sha256"] = exported["layout_profile"].get("layout_sha256")
    return exported


def _layout_source_graph(baseline_graph, materialized_graph):
    """Build the display union used by candidate, reference and diff views."""
    nodes = {}
    edges = {}
    for graph in (baseline_graph or {}, materialized_graph or {}):
        for node in graph.get("nodes", []):
            nodes[str(node.get("id"))] = {
                "id": str(node.get("id")), "label": str(node.get("label") or ""),
                "description": str(node.get("description") or ""),
                "origin": str(node.get("origin") or node.get("status") or ""),
                "baseline_refs": [str(ref) for ref in (node.get("baseline_refs") or [])],
            }
        for edge in graph.get("edges", []):
            edge_id = str(edge.get("id"))
            edges[edge_id] = {
                "id": edge_id, "source": str(edge.get("source") or ""),
                "target": str(edge.get("target") or ""),
                "condition": str(edge.get("label") or edge.get("condition") or ""),
                "origin": str(edge.get("origin") or edge.get("status") or ""),
                "baseline_refs": [str(ref) for ref in (edge.get("baseline_refs") or [])],
            }
    edges = {key: value for key, value in edges.items()
             if value["source"] in nodes and value["target"] in nodes}
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _layout_source_hash(baseline_graph, materialized_graph):
    return _stable_fingerprint(_layout_source_graph(baseline_graph, materialized_graph))


def _graph_layout_context(db, task_id, graph_id):
    task = _task_for(db, task_id)
    if not task:
        return None, {"error": "task_not_found"}
    graph = db.get("knowledge", {}).get(graph_id)
    if not graph or graph.get("task_id") != task_id or graph.get("type") != "graph":
        return None, {"error": "graph_not_found"}
    source = db.get("sources", {}).get(graph.get("source_id") or task.get("source_id")) or {}
    baseline, error = _selected_baseline(db, source, (graph.get("linkage") or {}).get("baseline_id"))
    if error:
        return None, error
    materialized = materialize_incremental_graph(db, graph, _graph_change_objects(db, graph), baseline)
    baseline_graph = (baseline or {}).get("graph") or {"nodes": [], "edges": []}
    layout_source = _layout_source_graph(baseline_graph, materialized)
    return {
        "task": task, "graph": graph, "baseline": baseline,
        "materialized": materialized, "layout_source": layout_source,
        "materialized_graph_hash": _stable_fingerprint(layout_source),
        "node_ids": {node["id"] for node in layout_source["nodes"]},
        "edge_ids": {edge["id"] for edge in layout_source["edges"]},
    }, None


def _layout_profile_for(db, task_id, graph_id=None, baseline_id=None):
    matches = [profile for profile in db.get("graph_layout_profiles", [])
               if profile.get("task_id") == task_id
               and ((graph_id and profile.get("graph_id") == graph_id)
                    or (not graph_id and baseline_id and not profile.get("graph_id")
                        and profile.get("baseline_id") == baseline_id))]
    return matches[-1] if matches else None


def _layout_profile_hash(profile):
    return _stable_fingerprint({
        "schema_version": 1, "phases": GRAPH_LAYOUT_PHASES,
        "auto_nodes": profile.get("auto_nodes") or {},
        "auto_edges": profile.get("auto_edges") or {},
        "manual_nodes": profile.get("manual_nodes") or {},
        "manual_edges": profile.get("manual_edges") or {},
    })


def _safe_layout_model_name():
    try:
        return llm_client.load_config().get("model") if llm_client else None
    except Exception:
        return None


def _layout_response(context, profile=None):
    baseline_id = (context.get("baseline") or {}).get("baseline_id")
    current_hash = context["materialized_graph_hash"]
    profile = profile or {}
    auto_nodes = profile.get("auto_nodes") or {}
    auto_edges = profile.get("auto_edges") or {}
    manual_nodes = profile.get("manual_nodes") or {}
    manual_edges = profile.get("manual_edges") or {}
    node_annotations = {}
    for node_id in sorted(context["node_ids"]):
        automatic = auto_nodes.get(node_id) or {}
        manual = manual_nodes.get(node_id) or {}
        phase_id = manual.get("phase_id") or automatic.get("phase_id") or "unassigned"
        node_annotations[node_id] = {
            "phase_id": phase_id if phase_id in GRAPH_LAYOUT_PHASE_IDS else "unassigned",
            "lane_override": manual.get("lane_override") if manual.get("lane_override") in GRAPH_LAYOUT_LANES else None,
            "source": "manual" if manual else "llm" if automatic else "unassigned",
        }
    edge_annotations = {}
    for edge_id in sorted(context["edge_ids"]):
        automatic = auto_edges.get(edge_id) or {}
        manual = manual_edges.get(edge_id) or {}
        tendency = manual.get("route_tendency") or automatic.get("route_tendency") or "unknown"
        edge_annotations[edge_id] = {
            "route_tendency": tendency if tendency in GRAPH_LAYOUT_TENDENCIES else "unknown",
            "source": "manual" if manual else "llm" if automatic else "unassigned",
        }
    stored_hash = profile.get("materialized_graph_hash")
    status = profile.get("status") or "missing"
    if profile and stored_hash != current_hash:
        status = "stale"
    response = {
        "layout_id": profile.get("layout_id"), "task_id": context["task"].get("task_id"),
        "graph_id": context["graph"].get("object_id"), "baseline_id": baseline_id,
        "materialized_graph_hash": current_hash, "profile_graph_hash": stored_hash,
        "status": status, "editable": True, "phases": GRAPH_LAYOUT_PHASES,
        "node_annotations": node_annotations, "edge_annotations": edge_annotations,
        "auto_nodes": auto_nodes, "auto_edges": auto_edges,
        "manual_nodes": manual_nodes, "manual_edges": manual_edges,
        "analysis": profile.get("analysis") or {}, "last_error": profile.get("last_error"),
    }
    response["layout_sha256"] = _layout_profile_hash(response)
    return response


def get_graph_layout(task_id, graph_id):
    db = _load_db()
    context, error = _graph_layout_context(db, task_id, graph_id)
    if error:
        return error
    profile = _layout_profile_for(db, task_id, graph_id=graph_id)
    if not profile and context.get("baseline"):
        profile = _layout_profile_for(
            db, task_id, baseline_id=context["baseline"].get("baseline_id")
        )
    return _layout_response(context, profile)


def _save_layout_profile(db, context, profile):
    profiles = db.setdefault("graph_layout_profiles", [])
    existing = _layout_profile_for(db, context["task"].get("task_id"), graph_id=context["graph"].get("object_id"))
    if existing:
        existing.clear()
        existing.update(profile)
        return existing
    profiles.append(profile)
    return profile


def _persist_layout_analysis(context, profile):
    """Reload before the post-LLM write so a long call cannot overwrite newer DB changes."""
    fresh_db = _load_db()
    fresh_context, error = _graph_layout_context(
        fresh_db, context["task"].get("task_id"), context["graph"].get("object_id")
    )
    if error:
        return None, None, error
    if fresh_context["materialized_graph_hash"] != profile.get("materialized_graph_hash"):
        return None, None, {"error": "layout_stale", "materialized_graph_hash": fresh_context["materialized_graph_hash"]}
    current = _layout_profile_for(
        fresh_db, context["task"].get("task_id"), graph_id=context["graph"].get("object_id")
    )
    if current:
        profile["manual_nodes"] = dict(current.get("manual_nodes") or {})
        profile["manual_edges"] = dict(current.get("manual_edges") or {})
    saved = _save_layout_profile(fresh_db, fresh_context, profile)
    _save_db(fresh_db)
    return fresh_context, saved, None


def analyze_graph_layout(task_id, graph_id, reviewer="system"):
    db = _load_db()
    context, error = _graph_layout_context(db, task_id, graph_id)
    if error:
        return error
    previous = _layout_profile_for(db, task_id, graph_id=graph_id)
    if previous and previous.get("status") == "ready" and previous.get("materialized_graph_hash") == context["materialized_graph_hash"]:
        result = _layout_response(context, previous)
        result["deduplicated"] = True
        return result
    seed = previous
    if not seed and context.get("baseline"):
        seed = _layout_profile_for(db, task_id, baseline_id=context["baseline"].get("baseline_id"))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    profile = {
        "layout_id": (previous or {}).get("layout_id") or _new_id("layout"),
        "task_id": task_id, "graph_id": graph_id,
        "baseline_id": (context.get("baseline") or {}).get("baseline_id"),
        "materialized_graph_hash": context["materialized_graph_hash"],
        "status": "running", "auto_nodes": {}, "auto_edges": {},
        "manual_nodes": dict((seed or {}).get("manual_nodes") or {}),
        "manual_edges": dict((seed or {}).get("manual_edges") or {}),
        "analysis": {"prompt_version": GRAPH_LAYOUT_PROMPT_VERSION, "started_at": now},
        "created_at": (previous or {}).get("created_at") or now, "updated_at": now,
    }
    _save_layout_profile(db, context, profile)
    _save_db(db)
    if not llm_client:
        profile["status"] = "failed"
        profile["last_error"] = {"error": "llm_client_not_available"}
        profile["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_db(db)
        return {"error": "llm_client_not_available", "layout": _layout_response(context, profile)}
    prompt_payload = context["layout_source"]
    system_prompt = """你是电话策略 Graph 的展示布局分类器。你只做展示标注，不修改策略语义。
把每个节点分到固定七阶段之一，把每条边按候选人反应方向标成 resistant、neutral、receptive 或 unknown。
unknown 不得猜成 resistant。顺序衔接、信息不足、多入口或难以判断时使用 neutral/unknown。
只允许返回输入中已有的稳定 ID，不得新增、改写、遗漏可判断的节点和边。
只返回 JSON：{"nodes":[{"node_id":"","phase_id":"pre_call|connect_permission|availability_routing|intent_objection|needs_matching|conversion|closure_followup"}],"edges":[{"edge_id":"","route_tendency":"resistant|neutral|receptive|unknown"}],"uncertainties":[""]}。"""
    try:
        result = llm_client.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps({"phases": GRAPH_LAYOUT_PHASES, **prompt_payload}, ensure_ascii=False, indent=2)},
        ], max_tokens=65536, thinking={"type": "enabled"})
    except Exception as exc:
        result = {"error": "llm_exception", "message": str(exc)}
    if "error" in result:
        profile["status"] = "failed"
        profile["last_error"] = {"error": "llm_call_failed", "detail": result}
        profile["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        final_context, final_profile, persist_error = _persist_layout_analysis(context, profile)
        if persist_error:
            return persist_error
        return {"error": "llm_call_failed", "detail": result, "layout": _layout_response(final_context, final_profile)}
    raw = result.get("content", "")
    try:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("JSON object missing")
        parsed = json.loads(raw[start:end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("layout JSON must be an object")
    except (ValueError, json.JSONDecodeError) as exc:
        profile["status"] = "failed"
        profile["last_error"] = {"error": "llm_parse_failed", "message": str(exc), "raw": raw[:500]}
        profile["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        final_context, final_profile, persist_error = _persist_layout_analysis(context, profile)
        if persist_error:
            return persist_error
        return {"error": "llm_parse_failed", "message": str(exc), "layout": _layout_response(final_context, final_profile)}
    warnings = []
    seen = set()
    auto_nodes = {}
    for item in parsed.get("nodes") or []:
        if not isinstance(item, dict):
            warnings.append({"type": "invalid_node_assignment", "node_id": ""})
            continue
        node_id, phase_id = str(item.get("node_id") or ""), str(item.get("phase_id") or "")
        if node_id in seen or node_id not in context["node_ids"] or phase_id not in GRAPH_LAYOUT_PHASE_IDS:
            warnings.append({"type": "invalid_node_assignment", "node_id": node_id})
            continue
        seen.add(node_id)
        auto_nodes[node_id] = {"phase_id": phase_id}
    seen = set()
    auto_edges = {}
    for item in parsed.get("edges") or []:
        if not isinstance(item, dict):
            warnings.append({"type": "invalid_edge_assignment", "edge_id": ""})
            continue
        edge_id, tendency = str(item.get("edge_id") or ""), str(item.get("route_tendency") or "")
        if edge_id in seen or edge_id not in context["edge_ids"] or tendency not in GRAPH_LAYOUT_TENDENCIES:
            warnings.append({"type": "invalid_edge_assignment", "edge_id": edge_id})
            continue
        seen.add(edge_id)
        auto_edges[edge_id] = {"route_tendency": tendency}
    profile.update({
        "status": "ready", "auto_nodes": auto_nodes, "auto_edges": auto_edges,
        "last_error": None, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "analysis": {
            "prompt_version": GRAPH_LAYOUT_PROMPT_VERSION,
            "model": (_safe_layout_model_name()),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "uncertainties": list(parsed.get("uncertainties") or []), "warnings": warnings,
        },
    })
    profile["layout_sha256"] = _layout_profile_hash(profile)
    final_context, final_profile, persist_error = _persist_layout_analysis(context, profile)
    if persist_error:
        return persist_error
    return _layout_response(final_context, final_profile)


def save_graph_layout(task_id, graph_id, materialized_graph_hash, node_updates=None,
                      edge_updates=None, editor="admin"):
    db = _load_db()
    context, error = _graph_layout_context(db, task_id, graph_id)
    if error:
        return error
    if materialized_graph_hash != context["materialized_graph_hash"]:
        return {"error": "layout_stale", "materialized_graph_hash": context["materialized_graph_hash"]}
    if not isinstance(node_updates or [], list) or not isinstance(edge_updates or [], list):
        return {"error": "invalid_layout_updates"}
    profile = _layout_profile_for(db, task_id, graph_id=graph_id)
    seed = profile
    if not seed and context.get("baseline"):
        seed = _layout_profile_for(db, task_id, baseline_id=context["baseline"].get("baseline_id"))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not profile:
        profile = {
            "layout_id": _new_id("layout"), "task_id": task_id, "graph_id": graph_id,
            "baseline_id": (context.get("baseline") or {}).get("baseline_id"),
            "materialized_graph_hash": context["materialized_graph_hash"],
            "status": "partial", "auto_nodes": dict((seed or {}).get("auto_nodes") or {}),
            "auto_edges": dict((seed or {}).get("auto_edges") or {}),
            "manual_nodes": dict((seed or {}).get("manual_nodes") or {}),
            "manual_edges": dict((seed or {}).get("manual_edges") or {}),
            "analysis": dict((seed or {}).get("analysis") or {}), "created_at": now,
        }
        db.setdefault("graph_layout_profiles", []).append(profile)
    before = _layout_profile_hash(profile)
    for update in node_updates or []:
        node_id = str(update.get("node_id") or "")
        if node_id not in context["node_ids"]:
            return {"error": "graph_node_not_found", "node_id": node_id}
        if update.get("clear_manual") is True:
            profile.setdefault("manual_nodes", {}).pop(node_id, None)
            continue
        phase_id = str(update.get("phase_id") or "")
        lane = update.get("lane_override") or None
        if phase_id not in GRAPH_LAYOUT_PHASE_IDS or (lane is not None and lane not in GRAPH_LAYOUT_LANES):
            return {"error": "invalid_layout_annotation", "node_id": node_id}
        profile.setdefault("manual_nodes", {})[node_id] = {"phase_id": phase_id, "lane_override": lane}
    for update in edge_updates or []:
        edge_id = str(update.get("edge_id") or "")
        if edge_id not in context["edge_ids"]:
            return {"error": "graph_edge_not_found", "edge_id": edge_id}
        if update.get("clear_manual") is True:
            profile.setdefault("manual_edges", {}).pop(edge_id, None)
            continue
        tendency = str(update.get("route_tendency") or "")
        if tendency not in GRAPH_LAYOUT_TENDENCIES:
            return {"error": "invalid_layout_annotation", "edge_id": edge_id}
        profile.setdefault("manual_edges", {})[edge_id] = {"route_tendency": tendency}
    profile.update({
        "materialized_graph_hash": context["materialized_graph_hash"],
        "updated_at": now, "updated_by": str(editor or "admin"),
    })
    profile["layout_sha256"] = _layout_profile_hash(profile)
    db.setdefault("access_audit", []).append({
        "audit_id": _new_id("aud"), "actor": str(editor or "admin"), "role": "reviewer",
        "scope": "graph_layout", "object_id": profile["layout_id"],
        "action": "update_graph_layout", "data_level": "D1", "result": "saved",
        "reason": f"{before} -> {profile['layout_sha256']}", "timestamp": now, "immutable": True,
    })
    _save_db(db)
    return _layout_response(context, profile)


def _portable_layout_profile(layout, node_ids, edge_ids):
    portable = {
        "format": "ai-call-graph-layout", "version": 1,
        "status": layout.get("status") if layout.get("status") in ("ready", "partial") else "partial",
        "phases": GRAPH_LAYOUT_PHASES,
        "auto_nodes": {key: value for key, value in (layout.get("auto_nodes") or {}).items() if key in node_ids},
        "auto_edges": {key: value for key, value in (layout.get("auto_edges") or {}).items() if key in edge_ids},
        "manual_nodes": {key: value for key, value in (layout.get("manual_nodes") or {}).items() if key in node_ids},
        "manual_edges": {key: value for key, value in (layout.get("manual_edges") or {}).items() if key in edge_ids},
    }
    portable["layout_sha256"] = _stable_fingerprint(portable)
    return portable


def _normalize_portable_layout(layout, node_ids, edge_ids):
    if not isinstance(layout, dict):
        return None, [{"type": "invalid_layout_profile"}]
    warnings = []
    supplied_hash = layout.get("layout_sha256")
    payload = {key: layout.get(key) for key in ("format", "version", "status", "phases", "auto_nodes", "auto_edges", "manual_nodes", "manual_edges")}
    if supplied_hash and supplied_hash != _stable_fingerprint(payload):
        return None, [{"type": "layout_hash_mismatch"}]
    result = {"status": layout.get("status") if layout.get("status") in ("ready", "partial") else "partial", "auto_nodes": {}, "auto_edges": {}, "manual_nodes": {}, "manual_edges": {}}
    for bucket in ("auto_nodes", "manual_nodes"):
        values = layout.get(bucket) or {}
        if not isinstance(values, dict):
            warnings.append({"type": "invalid_layout_bucket", "bucket": bucket})
            continue
        for node_id, value in values.items():
            phase_id = str((value or {}).get("phase_id") or "") if isinstance(value, dict) else ""
            lane = (value or {}).get("lane_override") if isinstance(value, dict) else None
            if node_id not in node_ids or phase_id not in GRAPH_LAYOUT_PHASE_IDS or (lane and lane not in GRAPH_LAYOUT_LANES):
                warnings.append({"type": "invalid_node_assignment", "node_id": node_id})
                continue
            result[bucket][node_id] = {"phase_id": phase_id, **({"lane_override": lane} if lane else {})}
    for bucket in ("auto_edges", "manual_edges"):
        values = layout.get(bucket) or {}
        if not isinstance(values, dict):
            warnings.append({"type": "invalid_layout_bucket", "bucket": bucket})
            continue
        for edge_id, value in values.items():
            tendency = str((value or {}).get("route_tendency") or "") if isinstance(value, dict) else ""
            if edge_id not in edge_ids or tendency not in GRAPH_LAYOUT_TENDENCIES:
                warnings.append({"type": "invalid_edge_assignment", "edge_id": edge_id})
                continue
            result[bucket][edge_id] = {"route_tendency": tendency}
    return result, warnings


def confirm_graph_match(task_id, baseline_id, candidate_id, reference_id, reviewer="admin", decision="confirmed", reason=""):
    db = _load_db()
    baseline = next((item for item in db.get("graph_baselines", []) if item.get("baseline_id") == baseline_id), None)
    if not baseline or baseline.get("task_id") != task_id:
        return {"error": "baseline_not_found"}
    if decision not in ("confirmed", "not_match", "deferred"):
        return {"error": "invalid_match_decision"}
    record = {
        "match_id": _new_id("gmatch"), "task_id": task_id, "baseline_id": baseline_id,
        "candidate_id": candidate_id, "reference_id": reference_id, "reviewer": reviewer,
        "decision": decision, "reason": reason, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")
    }
    db.setdefault("graph_match_confirmations", []).append(record)
    _save_db(db)
    audit_event(reviewer, "reviewer", f"task:{task_id}", candidate_id, "graph_match_confirmation", decision, reason)
    return record


def get_knowledge_object(obj_id):
    db = _load_db()
    return db.get("knowledge", {}).get(obj_id)


def new_version(obj_id, new_content, evidence_refs=None, reason=""):
    """C-01: 已批准对象不可原地修改，生成新版本并标记 supersedes"""
    db = _load_db()
    obj = db.get("knowledge", {}).get(obj_id)
    if not obj:
        return {"error": "object_not_found"}

    if obj["status"] != "approved":
        return {"error": "not_approved", "message": "只能对已批准对象生成新版本"}

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    new_id = _new_id("ko")
    new_ver = obj["version"] + 1

    new_obj = dict(obj)
    new_obj["object_id"] = new_id
    new_obj["version"] = new_ver
    new_obj["parent_version"] = obj["object_id"]
    new_obj["supersedes"] = obj["object_id"]
    new_obj["content"] = new_content
    new_obj["evidence_refs"] = evidence_refs or obj["evidence_refs"]
    new_obj["status"] = "candidate"
    new_obj["updated_at"] = now
    new_obj["immutable"] = False

    db["knowledge"][new_id] = new_obj
    _save_db(db)
    return new_obj


# ── C-02: 候选知识与基线 diff ───────────────────────
def _structural_diff(base, candidate):
    """Compare graph/script relations independently from text content."""
    old_link = base.get("linkage", {}) or {}
    new_link = candidate.get("linkage", {}) or {}
    result = {}
    for key in ("node_ids", "edge_ids", "trigger_ids", "stop_conditions", "script_type", "node_id"):
        old = old_link.get(key, [])
        new = new_link.get(key, [])
        if isinstance(old, list) or isinstance(new, list):
            old_set, new_set = set(old or []), set(new or [])
            result[key] = {"added": sorted(new_set - old_set),
                           "removed": sorted(old_set - new_set)}
        elif old != new:
            result[key] = {"old": old, "new": new}
    return result


def diff_against_baseline(obj_id):
    """C-02: 将候选对象与已批准基线比较"""
    db = _load_db()
    obj = db.get("knowledge", {}).get(obj_id)
    if not obj:
        return {"error": "object_not_found"}

    # 找基线：同 scope + 同 type 的已批准对象
    baseline = list_knowledge(status="approved", obj_type=obj["type"])

    diffs = []
    baseline = [b for b in baseline if b.get("scope") == obj["scope"]]
    for base in baseline:
        if obj.get("supersedes") == base["object_id"]:
            # 直接版本继承
            content_diff = _content_diff(base["content"], obj["content"])
            entry = {
                "baseline_id": base["object_id"],
                "baseline_version": base["version"],
                "baseline_content": base["content"],
                "candidate_content": obj["content"],
                "diff": content_diff,
                "relationship": "supersedes",
                "evidence_refs": obj["evidence_refs"],
            }
            if obj.get("type") in ("graph", "fragment", "overlay", "strategy_script_group", "script", "script_fragment"):
                entry["structural_diff"] = _structural_diff(base, obj)
            diffs.append(entry)
        else:
            # 同类基线比较
            entry = {
                "baseline_id": base["object_id"],
                "baseline_version": base["version"],
                "baseline_content": base["content"],
                "candidate_content": obj["content"],
                "diff": _content_diff(base["content"], obj["content"]),
                "relationship": "parallel",
                "evidence_refs": obj["evidence_refs"],
            }
            if obj.get("type") in ("graph", "fragment", "overlay", "strategy_script_group", "script", "script_fragment"):
                entry["structural_diff"] = _structural_diff(base, obj)
            diffs.append(entry)

    # 影响范围摘要
    impact = {
        "scope": obj["scope"],
        "type": obj["type"],
        "related_baselines": len(diffs),
        "has_conflict": obj.get("conflict_set") is not None,
        "evidence_count": len(obj.get("evidence_refs", [])),
    }

    return {"object_id": obj_id, "diffs": diffs, "impact": impact}


def _content_diff(old_text, new_text):
    """简单文本 diff：按行比较"""
    old_lines = old_text.split("\n") if old_text else []
    new_lines = new_text.split("\n") if new_text else []
    result = []
    max_len = max(len(old_lines), len(new_lines))
    for i in range(max_len):
        old = old_lines[i] if i < len(old_lines) else ""
        new = new_lines[i] if i < len(new_lines) else ""
        if old != new:
            result.append({"line": i, "old": old, "new": new})
    return result


# ── C-03: 变更提案 ────────────────────────────────
def create_change_proposal(task_id, change_type, target_object_id=None,
                           new_object=None, baseline_id=None, reason="",
                           evidence_refs=None, scope="general"):
    """C-03: 创建变更提案"""
    db = _load_db()
    if "changes" not in db:
        db["changes"] = []

    if change_type not in CHANGE_TYPES:
        return {"error": "invalid_change_type", "allowed": CHANGE_TYPES}

    # 结构变更必须有基线
    if change_type in STRUCTURAL_CHANGES and not baseline_id:
        return {"error": "missing_baseline",
                "message": "结构变更类型必须有基线对象"}

    # 必须有证据回链
    if not evidence_refs:
        return {"error": "missing_evidence",
                "message": "变更提案必须有证据回链"}

    change_id = _new_id("chg")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    # 获取旧对象（如果有）
    old_object = None
    if target_object_id:
        old_object = db.get("knowledge", {}).get(target_object_id)
        if not old_object:
            return {"error": "target_not_found"}

    # 基线对象
    baseline = None
    if baseline_id:
        baseline = db.get("knowledge", {}).get(baseline_id)
        if not baseline:
            return {"error": "baseline_not_found"}

    change = {
        "change_id": change_id,
        "change_type": change_type,
        "task_id": task_id,
        "target_object_id": target_object_id,
        "new_object": new_object,
        "baseline_id": baseline_id,
        "reason": reason,
        "evidence_refs": evidence_refs,
        "scope": scope,
        "status": "candidate",  # candidate -> pending_review -> approved/rejected/archived
        "diff": None,
        "impact_scope": None,
        "block_reason": None,
        "gate": None,  # 关联的 Gate
        "created_at": now,
        "updated_at": now,
    }

    # 计算 diff
    if old_object and new_object:
        change["diff"] = _content_diff(
            old_object.get("content", ""),
            new_object.get("content", ""))
        change["impact_scope"] = {
            "scope": old_object.get("scope"),
            "type": old_object.get("type"),
        }

    db["changes"].append(change)
    _save_db(db)
    return change


def list_changes(task_id=None, status=None, change_type=None):
    """C-03: 查询变更提案"""
    db = _load_db()
    result = db.get("changes", [])
    if task_id:
        result = [c for c in result if c.get("task_id") == task_id]
    if status:
        result = [c for c in result if c.get("status") == status]
    if change_type:
        result = [c for c in result if c.get("change_type") == change_type]
    return result


def get_change(change_id):
    db = _load_db()
    for c in db.get("changes", []):
        if c["change_id"] == change_id:
            return c
    return None


def materialize_incremental_graph(db, graph_object, change_objects, baseline):
    """Apply approved candidate changes to the exact immutable baseline for compilation."""
    baseline_graph = (baseline or {}).get("graph") or {"nodes": [], "edges": [], "triggers": []}
    indexes = _baseline_indexes(baseline)
    node_changes = [item for item in change_objects if item.get("type") == "strategy_node"]
    edge_changes = [item for item in change_objects if item.get("type") == "strategy_edge"]
    trigger_changes = [item for item in change_objects if item.get("type") == "strategy_trigger"]

    normalized_node_changes = []
    ref_counts = {}
    for item in node_changes:
        linkage = item.get("linkage") or {}
        refs, _ = _normalize_baseline_refs(
            linkage.get("baseline_refs"), linkage.get("baseline_match"), indexes["nodes"]
        )
        for ref in refs:
            ref_counts[ref] = ref_counts.get(ref, 0) + 1
        normalized_node_changes.append((item, refs))

    nodes = {}
    endpoint_map = {}
    changed_node_refs = {ref for _, refs in normalized_node_changes for ref in refs}
    for raw in baseline_graph.get("nodes", []):
        node_id = str(raw.get("id"))
        if node_id in changed_node_refs:
            continue
        nodes[node_id] = dict(raw, id=node_id, origin="baseline", change_type="unchanged")
        endpoint_map[node_id] = node_id

    script_selection_issues = []
    for item, refs in normalized_node_changes:
        linkage = item.get("linkage") or {}
        change_type = linkage.get("change_type") or ("split" if any(ref_counts.get(ref, 0) > 1 for ref in refs) else "modify" if refs else "add")
        if change_type == "deprecate":
            continue
        node_id = item.get("object_id")
        inherited = dict(indexes["nodes"]["by_id"].get(refs[0]) or {}) if len(refs) == 1 else {}
        exact_utterances, selection_issues = _resolve_node_script_examples(
            db, item, graph_object.get("object_id")
        )
        for issue in selection_issues:
            script_selection_issues.append(dict(issue, node_id=node_id))
        node_record = dict(inherited)
        node_record.update({
            "id": node_id,
            "label": item.get("content", ""),
            "origin": "candidate_change",
            "change_type": change_type,
            "baseline_refs": refs,
            "change_reason": linkage.get("change_reason", ""),
            "evidence_refs": sorted(set(
                [str(ref) for ref in (inherited.get("evidence_refs") or [])]
                + normalize_evidence_refs(db, item.get("evidence_refs", []))
            )),
            "context_refs": sorted(set(
                [str(ref) for ref in (inherited.get("context_refs") or [])]
                + normalize_evidence_refs(db, linkage.get("context_refs", []))
            )),
        })
        if exact_utterances:
            node_record["expert_utterances"] = exact_utterances
        else:
            node_record.setdefault("expert_utterances", [])
        node_record.setdefault("scripts", [])
        node_record.setdefault("script_ids", [])
        node_record.setdefault("metadata", {})
        nodes[node_id] = node_record
        endpoint_map[item.get("object_id")] = node_id
        endpoint_map[linkage.get("candidate_key")] = node_id
        for ref in refs:
            endpoint_map.setdefault(ref, node_id)

    changed_edge_refs = set()
    normalized_edge_changes = []
    for item in edge_changes:
        linkage = item.get("linkage") or {}
        refs, _ = _normalize_baseline_refs(
            linkage.get("baseline_refs"), linkage.get("baseline_match"), indexes["edges"]
        )
        changed_edge_refs.update(refs)
        normalized_edge_changes.append((item, refs))
    edges = {}
    for raw in baseline_graph.get("edges", []):
        edge_id = str(raw.get("id"))
        if edge_id in changed_edge_refs:
            continue
        source = endpoint_map.get(str(raw.get("source") or raw.get("from_node_id") or raw.get("from") or ""))
        target = endpoint_map.get(str(raw.get("target") or raw.get("to_node_id") or raw.get("to") or ""))
        if source and target:
            edges[edge_id] = dict(raw, id=edge_id, source=source, target=target, origin="baseline", change_type="unchanged")
    for item, refs in normalized_edge_changes:
        linkage = item.get("linkage") or {}
        change_type = linkage.get("change_type") or ("modify" if refs else "add")
        if change_type == "deprecate":
            continue
        inherited = dict(indexes["edges"]["by_id"].get(refs[0]) or {}) if len(refs) == 1 else {}
        source_ref = str(linkage.get("from_ref") or linkage.get("from_node_id") or "")
        target_ref = str(linkage.get("to_ref") or linkage.get("to_node_id") or "")
        source = endpoint_map.get(source_ref, source_ref if source_ref in nodes else None)
        target = endpoint_map.get(target_ref, target_ref if target_ref in nodes else None)
        if source and target:
            condition = (linkage.get("condition") or linkage.get("extracted_condition")
                         or item.get("content", ""))
            edge_record = dict(inherited)
            edge_record.update({
                "id": item.get("object_id"), "source": source, "target": target,
                "label": condition, "condition": condition,
                "origin": "candidate_change", "change_type": change_type,
                "baseline_refs": refs, "change_reason": linkage.get("change_reason", ""),
                "evidence_refs": sorted(set(
                    [str(ref) for ref in (inherited.get("evidence_refs") or [])]
                    + normalize_evidence_refs(db, item.get("evidence_refs", []))
                )),
                "context_refs": sorted(set(
                    [str(ref) for ref in (inherited.get("context_refs") or [])]
                    + normalize_evidence_refs(db, linkage.get("context_refs", []))
                )),
                "extracted_condition": linkage.get("extracted_condition") or inherited.get("extracted_condition") or inherited.get("label") or condition,
                "condition_review_status": linkage.get("condition_review_status") or (
                    inherited.get("condition_review_status") or
                    ("needs_review" if linkage.get("condition_uncertainty") else "unreviewed")),
                "condition_uncertainty": linkage.get("condition_uncertainty") or inherited.get("condition_uncertainty", ""),
                "condition_reviewer": linkage.get("condition_reviewer") or inherited.get("condition_reviewer", ""),
                "condition_reviewed_at": linkage.get("condition_reviewed_at") or inherited.get("condition_reviewed_at", ""),
            })
            edge_record.setdefault("metadata", {})
            edges[item.get("object_id")] = edge_record

    changed_trigger_refs = set()
    normalized_trigger_changes = []
    for item in trigger_changes:
        linkage = item.get("linkage") or {}
        refs, _ = _normalize_baseline_refs(
            linkage.get("baseline_refs"), linkage.get("baseline_match"), indexes["triggers"]
        )
        changed_trigger_refs.update(refs)
        normalized_trigger_changes.append((item, refs))
    triggers = []
    for raw in baseline_graph.get("triggers", []):
        trigger_id = str(raw.get("id"))
        if trigger_id in changed_trigger_refs:
            continue
        target_ref = str(raw.get("target_node_id") or raw.get("target") or "")
        target = endpoint_map.get(target_ref, target_ref if target_ref in nodes else None)
        if target:
            triggers.append(dict(raw, id=trigger_id, target_node_id=target, origin="baseline", change_type="unchanged"))
    for item, refs in normalized_trigger_changes:
        linkage = item.get("linkage") or {}
        change_type = linkage.get("change_type") or ("modify" if refs else "add")
        if change_type == "deprecate":
            continue
        inherited = dict(indexes["triggers"]["by_id"].get(refs[0]) or {}) if len(refs) == 1 else {}
        target_ref = str(linkage.get("target_ref") or linkage.get("target_node_id") or "")
        target = endpoint_map.get(target_ref, target_ref if target_ref in nodes else None)
        if target:
            trigger_record = dict(inherited)
            trigger_record.update({
                "id": item.get("object_id"), "target_node_id": target,
                "label": linkage.get("condition") or item.get("content", ""),
                "origin": "candidate_change", "change_type": change_type,
                "baseline_refs": refs, "change_reason": linkage.get("change_reason", ""),
                "evidence_refs": sorted(set(
                    [str(ref) for ref in (inherited.get("evidence_refs") or [])]
                    + normalize_evidence_refs(db, item.get("evidence_refs", []))
                )),
                "context_refs": sorted(set(
                    [str(ref) for ref in (inherited.get("context_refs") or [])]
                    + normalize_evidence_refs(db, linkage.get("context_refs", []))
                )),
            })
            trigger_record.setdefault("metadata", {})
            triggers.append(trigger_record)

    graph_linkage = graph_object.get("linkage") or {}
    materialized = {
        "format": "ai-call-strategy-graph",
        "version": 1,
        "baseline_id": (baseline or {}).get("baseline_id"),
        "baseline_content_hash": (baseline or {}).get("content_hash"),
        "candidate_graph_id": graph_object.get("object_id"),
        "analysis_summary": graph_linkage.get("analysis_summary", ""),
        "uncertainties": graph_linkage.get("uncertainties", []),
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "triggers": triggers,
        "script_selection_issues": script_selection_issues,
        "stop_conditions": list(baseline_graph.get("stop_conditions") or graph_linkage.get("stop_conditions") or []),
    }
    classified, _ = classify_graph_edge_conditions(materialized)
    return classified



# ── E-01: approved-only 编译输入 ───────────────────
def collect_compile_input(source_id=None, task_id=None, scope="general",
                          strategy_script_group_id=None):
    """收集已批准对象作为编译输入，排除候选/冲突未决/未批准/跨组对象"""
    db = _load_db()
    
    all_objects = list(db.get("knowledge", {}).values())
    if task_id:
        all_objects = [obj for obj in all_objects if obj.get("task_id") == task_id]
    elif source_id:
        all_objects = [obj for obj in all_objects if obj.get("source_id") == source_id]
        task_ids = {obj.get("task_id") for obj in all_objects if obj.get("task_id")}
        task_id = next(iter(task_ids)) if len(task_ids) == 1 else None
    else:
        return {"error": "missing_task_scope", "message": "编译输入必须指定 task_id 或 source_id"}

    approved_graphs = [obj for obj in all_objects
                       if obj.get("status") == "approved" and obj.get("type") == "graph"]
    graph_groups = {obj.get("linkage", {}).get("group_id") for obj in approved_graphs
                    if obj.get("linkage", {}).get("group_id")}
    if not strategy_script_group_id and len(graph_groups) == 1:
        strategy_script_group_id = next(iter(graph_groups))
    if not strategy_script_group_id and len(graph_groups) > 1:
        return {"error": "strategy_group_required", "group_ids": sorted(graph_groups)}
    
    # 过滤条件
    approved = []
    blocked = []
    
    for obj in all_objects:
        # 必须是 approved
        if obj.get("status") != "approved":
            blocked.append({
                "object_id": obj.get("object_id"),
                "type": obj.get("type"),
                "status": obj.get("status"),
                "reason": "not_approved",
            })
            continue

        obj_group_id = obj.get("linkage", {}).get("group_id")
        if strategy_script_group_id and not (
            obj_group_id == strategy_script_group_id
            or obj.get("object_id") == strategy_script_group_id
        ):
            blocked.append({
                "object_id": obj.get("object_id"), "type": obj.get("type"),
                "status": obj.get("status"), "reason": "cross_group",
            })
            continue
        
        # scope 过滤
        if scope and obj.get("scope") != scope and obj.get("scope") != "general":
            blocked.append({
                "object_id": obj.get("object_id"),
                "type": obj.get("type"),
                "status": obj.get("status"),
                "reason": "scope_mismatch",
            })
            continue
        
        # 冲突未决
        if obj.get("conflict_set"):
            blocked.append({
                "object_id": obj.get("object_id"),
                "type": obj.get("type"),
                "status": obj.get("status"),
                "reason": "unresolved_conflict",
            })
            continue
        
        # source_id 过滤（如果指定）
        if source_id and obj.get("source_id") != source_id:
            blocked.append({
                "object_id": obj.get("object_id"),
                "type": obj.get("type"),
                "status": obj.get("status"),
                "reason": "cross_group",
            })
            continue
        
        # 证据回链检查。继承基线边的纯条件校准复用已锁定基线身份，
        # 可以没有新增访谈证据；该例外仍需逐项校验基线版本、哈希、端点与边引用。
        baseline_correction = (not obj.get("evidence_refs") and (
            (obj.get("type") == "strategy_edge"
             and _valid_baseline_condition_correction(db, task_id, obj.get("linkage") or {}))
            or (obj.get("type") == "strategy_node"
                and _valid_baseline_node_correction(db, task_id, obj.get("linkage") or {}))
        ))
        if not obj.get("evidence_refs") and not baseline_correction:
            blocked.append({
                "object_id": obj.get("object_id"),
                "type": obj.get("type"),
                "status": obj.get("status"),
                "reason": "no_evidence_refs",
            })
            continue
        
        approved.append(obj)
    
    # 按类型分组排序：Guard/政策 → 策略 → 话术 → 评分
    type_order = {
        "policy_guard": 0,
        "scoring_rule": 3,
        "strategy_node": 1,
        "script_fragment": 2,
    }
    approved.sort(key=lambda o: (type_order.get(o.get("type"), 99), o.get("object_id", "")))
    
    # 构建输入清单
    input_list = []
    for obj in approved:
        input_list.append({
            "object_id": obj["object_id"],
            "type": obj["type"],
            "version": obj.get("version", 1),
            "content": obj.get("content", ""),
            "evidence_refs": obj.get("evidence_refs", []),
            "scope": obj.get("scope", ""),
            "linkage": obj.get("linkage", {}),
        })
    
    return {
        "scope": scope,
        "task_id": task_id,
        "source_id": source_id,
        "strategy_script_group_id": strategy_script_group_id,
        "total_objects": len(all_objects),
        "approved_count": len(approved),
        "blocked_count": len(blocked),
        "input_list": input_list,
        "blocked": blocked,
    }



# ── E-02/E-03: 同源执行/评价 Prompt ──────────────────
def _approved_graph_compile_context(source_id=None, task_id=None, scope="general",
                                    strategy_script_group_id=None):
    ci = collect_compile_input(source_id, task_id, scope, strategy_script_group_id)
    if "error" in ci:
        return ci
    if ci["approved_count"] == 0:
        return {"error": "no_approved_objects", "message": "没有已批准对象可编译"}

    input_list = ci["input_list"]
    graphs = [o for o in input_list if o["type"] == "graph"]
    if not graphs:
        return {"error": "insufficient_evidence", "message": "approved Graph is required"}
    graph = graphs[0]
    graph_linkage = graph.get("linkage", {})
    node_ids = set(graph_linkage.get("node_ids", []))
    edge_ids = set(graph_linkage.get("edge_ids", []))
    trigger_ids = set(graph_linkage.get("trigger_ids", []))
    by_id = {o["object_id"]: o for o in input_list}
    strategy_nodes = [o for o in input_list if o["type"] == "strategy_node" and
                      (not node_ids or o["object_id"] in node_ids)]
    edges = [o for o in input_list if o["type"] == "strategy_edge" and
             (not edge_ids or o["object_id"] in edge_ids)]
    triggers = [o for o in input_list if o["type"] == "strategy_trigger" and
                (not trigger_ids or o["object_id"] in trigger_ids)]
    missing = sorted((node_ids | edge_ids | trigger_ids) - set(by_id))
    if missing:
        return {"error": "insufficient_evidence", "missing_object_ids": missing}
    db = _load_db()
    task = _task_for(db, task_id) if task_id else None
    source = db.get("sources", {}).get(source_id or (task or {}).get("source_id") or graph.get("source_id"), {})
    baseline, baseline_error = _selected_baseline(
        db, source, graph_linkage.get("baseline_id")
    ) if source else (None, None)
    if baseline_error:
        return baseline_error
    materialized_graph = materialize_incremental_graph(
        db, graph, strategy_nodes + edges + triggers, baseline
    )
    if not materialized_graph.get("nodes"):
        return {"error": "no_strategy_nodes", "message": "物化后的完整 Graph 没有节点"}
    if not materialized_graph.get("edges"):
        return {"error": "insufficient_evidence", "message": "物化后的完整 Graph 没有边"}
    condition_issues = materialized_graph.get("condition_issues") or []
    if condition_issues:
        return {
            "error": "missing_branch_conditions",
            "message": "完整 Graph 存在未确认、空白或互相冲突的问题边，处理前不能编译 Prompt",
            "condition_issues": condition_issues,
        }
    script_issues = materialized_graph.get("script_selection_issues") or []
    if script_issues:
        return {
            "error": "invalid_script_selections",
            "message": "完整 Graph 包含无效节点原话选择，修复前不能编译 Prompt",
            "issues": script_issues,
        }
    return {
        "compile_input": ci,
        "graph": graph,
        "strategy_objects": strategy_nodes + edges + triggers,
        "materialized_graph": materialized_graph,
    }


def build_authoritative_route_table(materialized_graph):
    """Deterministic routing appendix; canonical conditions are never paraphrased."""
    nodes = {str(node.get("id")): str(node.get("label") or node.get("name") or node.get("id") or "")
             for node in materialized_graph.get("nodes", [])}
    lines = ["# 权威路由表（条件原文，不得改写）"]
    for edge in sorted(materialized_graph.get("edges", []), key=lambda item: str(item.get("id") or "")):
        edge_id = str(edge.get("id") or edge.get("edge_id") or "")
        source = str(edge.get("source") or edge.get("from_node_id") or edge.get("from") or "")
        target = str(edge.get("target") or edge.get("to_node_id") or edge.get("to") or "")
        condition = str(edge.get("condition") or edge.get("condition_display") or
                        edge.get("label") or "").strip()
        if edge.get("condition_kind") == "implicit_sequence":
            condition = str(edge.get("condition_display") or "完成上一步后继续（原图无显式条件）")
        lines.extend([
            f"## 路由 {edge_id}",
            f"- 从：[{source}] {nodes.get(source, source)}",
            f"- 到：[{target}] {nodes.get(target, target)}",
            f"- 条件：{condition}",
            f"- 条件类型：{edge.get('condition_kind', 'explicit')}",
        ])
    return "\n".join(lines)


def _execution_node_text(materialized_graph):
    lines = []
    for node in sorted(materialized_graph.get("nodes", []), key=lambda item: str(item.get("id") or "")):
        node_id = str(node.get("id") or "")
        lines.append(f"## 节点 [{node_id}] {node.get('label') or node.get('name') or node_id}")
        examples = node.get("expert_utterances") or node.get("scripts") or []
        for example in examples:
            if isinstance(example, str):
                text, speaker, timestamp = example, "", ""
            else:
                text = str(example.get("text") or example.get("content") or "")
                speaker = str(example.get("speaker") or "")
                timestamp = str(example.get("timestamp") or "")
            if text:
                location = " · ".join(item for item in (speaker, timestamp) if item)
                lines.append(f"- 专家话术范例{f'（{location}）' if location else ''}：{text}")
    return "\n".join(lines)


def _execution_prompt_from_graph(materialized_graph, route_table):
    stops = materialized_graph.get("stop_conditions") or []
    stop_text = "\n".join(f"- {item}" for item in stops) or "- 未提供；这表示未知，不表示禁止或必须继续。"
    return """# 电话执行 Prompt｜专家克隆策略

你负责在真实电话对话中尽量忠实执行下方已批准的专家克隆 Graph。

## 路由解释原则
- 路由条件是需要结合完整对话理解的语义线索，不是关键词白名单。
- 条件描述可以是模糊或未穷尽的；未提供的信息表示未知，不得自动推导为否定、不适用、禁止或反向条件。
- 多条路线看起来都可能时，结合候选人的当前表达和完整上下文作合理判断；确有必要时自然澄清。
- 只能沿权威路由表中的路线推进，不得发明 Graph 中不存在的路线。

## Graph 身份
- candidate_graph_id: {graph_id}
- baseline_id: {baseline_id}
- baseline_content_hash: {baseline_hash}

## 策略节点与专家话术
{nodes}

## 停止条件
{stops}

{routes}
""".format(
        graph_id=materialized_graph.get("candidate_graph_id") or "",
        baseline_id=materialized_graph.get("baseline_id") or "",
        baseline_hash=materialized_graph.get("baseline_content_hash") or "",
        nodes=_execution_node_text(materialized_graph),
        stops=stop_text,
        routes=route_table,
    )


def generate_execution_prompt(source_id=None, task_id=None, scope="general",
                              strategy_script_group_id=None):
    context = _approved_graph_compile_context(source_id, task_id, scope, strategy_script_group_id)
    if "error" in context:
        return context
    graph = context["graph"]
    materialized_graph = context["materialized_graph"]
    route_table = build_authoritative_route_table(materialized_graph)
    route_hash = hashlib.sha256(route_table.encode("utf-8")).hexdigest()
    prompt = _execution_prompt_from_graph(materialized_graph, route_table)
    return {
        "prompt_type": "execution",
        "prompt_content": prompt,
        "input_objects": [graph["object_id"]] + [item["object_id"] for item in context["strategy_objects"]],
        "input_count": 1 + len(context["strategy_objects"]),
        "route_table": route_table,
        "route_table_sha256": route_hash,
        "materialized_graph": materialized_graph,
        "llm_model": "deterministic",
        "llm_usage": {},
        "llm_config_snapshot": llm_client.get_config_snapshot() if llm_client else {},
    }


def llm_generate_strategy_prompt(source_id=None, task_id=None, scope="general",
                                 strategy_script_group_id=None, **kwargs):
    """E-02: 用 LLM 基于已批准策略流程图生成策略流程评价 Prompt"""
    context = _approved_graph_compile_context(source_id, task_id, scope, strategy_script_group_id)
    if "error" in context:
        return context
    graph = context["graph"]
    strategy_objects = context["strategy_objects"]
    materialized_graph = context["materialized_graph"]
    route_table = build_authoritative_route_table(materialized_graph)
    route_hash = hashlib.sha256(route_table.encode("utf-8")).hexdigest()

    import json as _json
    structure_text = _json.dumps(materialized_graph, ensure_ascii=False, sort_keys=True)

    system_prompt = """你是一个评价规则生成助手。下面是由“精确基线 + 已批准增量变更”物化得到的完整猎头策略流程图。
请基于这张完整图生成一份"策略流程评价 Prompt"，用于评价 AI 猎头在电话中的策略走向是否正确。不得只关注本次变化节点，也不得丢弃 origin=baseline 的既有策略。

评价 Prompt 应包含：
1. 评价目标：判断 AI 猎头的策略走向是否符合已批准流程图
2. 策略节点清单：列出所有应执行的节点和分支条件
3. 评价维度：该走的节点走了没有、该触发的分支触发了没有、该停止时停止了没有
4. 条件按语义和完整上下文判断，不得当作关键词白名单；未提供的信息是未知，不得推成否定
5. 输出格式：逐项给出节点命中/未命中/无法判断，证据不足必须为“无法判断”，附理由

系统会在正文后确定性追加权威路由表；不要自行改写、补全或删减路由条件。
请直接输出评价 Prompt 正文，不要加额外说明。"""

    user_prompt = "已批准并物化的完整策略 Graph：\n" + structure_text

    if not llm_client:
        return {"error": "llm_client_not_available"}

    result = llm_client.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], max_tokens=kwargs.get("max_tokens", 65536))

    if "error" in result:
        return {"error": "llm_call_failed", "detail": result}

    prompt_content = result.get("content", "").rstrip() + "\n\n" + route_table
    return {
        "prompt_type": "strategy",
        "prompt_content": prompt_content,
        "input_objects": [graph["object_id"]] + [o["object_id"] for o in strategy_objects],
        "input_count": 1 + len(strategy_objects),
        "llm_model": result.get("model", ""),
        "llm_usage": result.get("usage", {}),
        "llm_config_snapshot": llm_client.get_config_snapshot(),
        "materialized_graph": materialized_graph,
        "materialized_node_count": len(materialized_graph.get("nodes", [])),
        "materialized_edge_count": len(materialized_graph.get("edges", [])),
        "route_table": route_table,
        "route_table_sha256": route_hash,
    }


def llm_generate_script_prompt(source_id=None, task_id=None, scope="general",
                               strategy_script_group_id=None, **kwargs):
    """E-03: 用 LLM 基于已批准话术生成话术评价 Prompt"""
    ci = collect_compile_input(source_id, task_id, scope, strategy_script_group_id)
    if "error" in ci:
        return ci
    if ci["approved_count"] == 0:
        return {"error": "no_approved_objects", "message": "没有已批准对象可编译"}

    # 只取话术片段
    scripts = [o for o in ci["input_list"] if o["type"] == "script_fragment"]
    if not scripts:
        return {"error": "no_scripts", "message": "已批准对象中无话术片段"}

    scripts_desc = []
    for s in scripts:
        linkage = s.get("linkage", {})
        scripts_desc.append("- 节点:{} 话术类型:{} 证据:{} 时间:{} 内容:{}".format(
            linkage.get("node_id", ""),
            linkage.get("script_type", ""),
            (s.get("evidence_refs") or [""])[0],
            linkage.get("timestamp", ""),
            s["content"]))
    
    scripts_text = "\n".join(scripts_desc)

    system_prompt = """你是一个评价规则生成助手。下面是一个猎头的已批准话术（附在策略节点上的原话/学习策略）。
请基于这些话术，生成一份"话术评价 Prompt"，用于评价 AI 猎头在对应节点上的表达质量。

评价 Prompt 应包含：
1. 评价目标：判断 AI 猎头的话术与标准话术的接近度
2. 话术标准清单：列出每个节点的标准话术和话术类型
3. 评价维度：说了什么、怎么说、用词和语感是否接近标准
4. 输出格式：逐节点给出话术接近度（高/中/低/未提及），附理由

请直接输出评价 Prompt 正文，不要加额外说明。"""

    user_prompt = "已批准话术：\n" + scripts_text

    if not llm_client:
        return {"error": "llm_client_not_available"}

    result = llm_client.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], max_tokens=kwargs.get("max_tokens", 65536))

    if "error" in result:
        return {"error": "llm_call_failed", "detail": result}

    return {
        "prompt_type": "script",
        "prompt_content": result.get("content", ""),
        "input_objects": [o["object_id"] for o in scripts],
        "input_count": len(scripts),
        "llm_model": result.get("model", ""),
        "llm_usage": result.get("usage", {}),
        "llm_config_snapshot": llm_client.get_config_snapshot(),
    }


# ── E-04: Prompt/manifest 可复现编译 ────────────────
COMPILER_VERSION = "v0.2-route"

def compile_release(source_id=None, task_id=None, scope="general",
                     prompt_size_budget=262144, strategy_script_group_id=None, **kwargs):
    """E-04: 编译电话执行 Prompt 与两种评价 Prompt，生成 manifest 和哈希。"""
    # 生成策略评价 Prompt
    strat = llm_generate_strategy_prompt(
        source_id, task_id, scope, strategy_script_group_id, **kwargs
    )
    if "error" in strat:
        return strat

    execution = generate_execution_prompt(source_id, task_id, scope, strategy_script_group_id)
    if "error" in execution:
        return execution
    if execution.get("route_table_sha256") != strat.get("route_table_sha256"):
        return {"error": "route_table_drift", "message": "执行与评价 Prompt 的权威路由表不一致"}

    # 生成话术评价 Prompt
    script = llm_generate_script_prompt(
        source_id, task_id, scope, strategy_script_group_id, **kwargs
    )
    if "error" in script:
        if script.get("error") not in ("no_scripts", "no_approved_objects"):
            return script
        # 如果没有话术，只编译策略 Prompt
        script = {"prompt_content": "(无已批准话术，话术评价 Prompt 省略)",
                   "input_objects": [], "input_count": 0,
                   "llm_model": "", "llm_usage": {},
                   "llm_config_snapshot": llm_client.get_config_snapshot()}

    # 合并 Prompt（策略 60% + 话术 40%）
    combined_prompt = (
        "# 策略流程评价 Prompt (60%)\n\n"
        + strat.get("prompt_content", "")
        + "\n\n# 话术评价 Prompt (40%)\n\n"
        + script.get("prompt_content", "")
    )

    # 检查超预算
    prompt_bytes = combined_prompt.encode("utf-8")
    execution_bytes = execution.get("prompt_content", "").encode("utf-8")
    prompt_size = len(prompt_bytes) + len(execution_bytes)
    if prompt_size > prompt_size_budget:
        return {"error": "over_budget",
                "prompt_size": prompt_size,
                "budget": prompt_size_budget,
                "message": "Prompt 超过预算，不静默截断"}

    # 收集输入对象
    ci = collect_compile_input(source_id, task_id, scope, strategy_script_group_id)
    if "error" in ci:
        return ci
    input_objects = [o["object_id"] for o in ci["input_list"]]
    db = _load_db()
    task = _task_for(db, task_id) if task_id else None
    source = db.get("sources", {}).get(source_id or (task or {}).get("source_id"), {})
    baseline, baseline_error = _selected_baseline(db, source) if source else (None, None)
    if baseline_error:
        return baseline_error

    # 生成哈希
    prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
    
    # manifest（不含自身哈希）
    import json as _json
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    compile_id = _new_id("cmp")
    
    manifest = {
        "compile_id": compile_id,
        "compiler_version": COMPILER_VERSION,
        "compiled_at": now,
        "source_id": source_id,
        "task_id": task_id,
        "strategy_script_group_id": ci.get("strategy_script_group_id"),
        "baseline_id": baseline.get("baseline_id") if baseline else None,
        "baseline_version": baseline.get("version") if baseline else None,
        "baseline_content_hash": baseline.get("content_hash") if baseline else None,
        "scope": scope,
        "input_objects": input_objects,
        "input_object_count": len(input_objects),
        "candidate_graph_id": execution.get("materialized_graph", {}).get("candidate_graph_id"),
        "execution_prompt_sha256": hashlib.sha256(execution_bytes).hexdigest(),
        "route_table_sha256": execution.get("route_table_sha256"),
        "strategy_prompt_sha256": hashlib.sha256(strat.get("prompt_content", "").encode("utf-8")).hexdigest(),
        "script_prompt_sha256": hashlib.sha256(script.get("prompt_content", "").encode("utf-8")).hexdigest(),
        "combined_prompt_sha256": prompt_sha256,
        "prompt_size": prompt_size,
        "prompt_size_budget": prompt_size_budget,
        "llm_config_snapshot": strat.get("llm_config_snapshot", {}),
        "llm_usage_strategy": strat.get("llm_usage", {}),
        "llm_usage_script": script.get("llm_usage", {}),
        "execution_compiler": "deterministic-route-v1",
    }
    
    # manifest 自身哈希（排除 manifest_sha256 字段）
    manifest_str = _json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    manifest_sha256 = hashlib.sha256(manifest_str.encode("utf-8")).hexdigest()
    manifest["manifest_sha256"] = manifest_sha256

    # 存储编译结果
    if "compilations" not in db:
        db["compilations"] = []
    
    compilation = {
        "compile_id": compile_id,
        "manifest": manifest,
        "execution_prompt": execution.get("prompt_content", ""),
        "route_table": execution.get("route_table", ""),
        "strategy_prompt": strat.get("prompt_content", ""),
        "script_prompt": script.get("prompt_content", ""),
        "combined_prompt": combined_prompt,
        "created_at": now,
    }
    db["compilations"].append(compilation)
    _save_db(db)

    return {
        "compile_id": compile_id,
        "manifest": manifest,
        "prompt_size": prompt_size,
        "prompt_sha256": prompt_sha256,
        "execution_prompt_preview": execution.get("prompt_content", "")[:200],
        "strategy_prompt_preview": strat.get("prompt_content", "")[:200],
        "script_prompt_preview": script.get("prompt_content", "")[:200],
    }


def get_compilation(compile_id):
    """获取编译结果"""
    db = _load_db()
    for c in db.get("compilations", []):
        if c["compile_id"] == compile_id:
            return c
    return None


def list_compilations(task_id=None):
    """列出所有编译"""
    db = _load_db()
    result = []
    for c in db.get("compilations", []):
        if task_id and c.get("manifest", {}).get("task_id") != task_id:
            continue
        result.append({
            "compile_id": c["compile_id"],
            "created_at": c.get("created_at", ""),
            "manifest": c.get("manifest", {}),
        })
    return result


# ── E-05: 发布包与核验卡 ──────────────────────────
def create_release_package(compile_id, release_owner="admin", scope="general"):
    """E-05: 创建发布包"""
    compilation = get_compilation(compile_id)
    if not compilation:
        return {"error": "compile_not_found"}

    db = _load_db()
    manifest = compilation.get("manifest", {})
    task_id = manifest.get("task_id")
    if not task_id or not approved_gate(db, task_id, "G5"):
        return {"error": "gate_required", "gate_id": "G5",
                "message": "release requires an approved G5 record"}
    if manifest.get("baseline_id"):
        baseline = next((item for item in db.get("graph_baselines", [])
                         if item.get("baseline_id") == manifest.get("baseline_id")
                         and item.get("task_id") == task_id), None)
        if (not baseline
                or baseline.get("version") != manifest.get("baseline_version")
                or baseline.get("content_hash") != manifest.get("baseline_content_hash")
                or baseline.get("content_hash") != _stable_fingerprint(baseline.get("graph", {}))):
            return {"error": "baseline_hash_mismatch", "baseline_id": manifest.get("baseline_id")}
    input_ids = manifest.get("input_objects", [])
    input_objects = [db.get("knowledge", {}).get(object_id)
                     for object_id in input_ids]
    if any(obj and obj.get("type") in ("policy_guard", "scoring_rule") for obj in input_objects):
        if not approved_gate(db, task_id, "G4"):
            return {"error": "gate_required", "gate_id": "G4",
                    "message": "policy or scoring inputs require G4"}
    combined = compilation.get("combined_prompt", "")
    expected_hash = manifest.get("combined_prompt_sha256", "")
    if expected_hash != hashlib.sha256(combined.encode("utf-8")).hexdigest():
        return {"error": "hash_mismatch", "field": "combined_prompt_sha256"}
    execution_prompt = compilation.get("execution_prompt", "")
    if (manifest.get("execution_prompt_sha256")
            and manifest.get("execution_prompt_sha256") != hashlib.sha256(execution_prompt.encode("utf-8")).hexdigest()):
        return {"error": "hash_mismatch", "field": "execution_prompt_sha256"}
    route_table = compilation.get("route_table", "")
    if (manifest.get("route_table_sha256")
            and manifest.get("route_table_sha256") != hashlib.sha256(route_table.encode("utf-8")).hexdigest()):
        return {"error": "hash_mismatch", "field": "route_table_sha256"}
    if "releases" not in db:
        db["releases"] = []

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    release_id = _new_id("rel")
    version = 1
    
    # 计算已有同 scope 的发布包数量来确定版本
    existing = [r for r in db["releases"]
                if r.get("scope") == scope
                and r.get("release_manifest", {}).get("task_id") == task_id]
    version = len(existing) + 1

    prompt_sha = manifest.get("combined_prompt_sha256", "")
    manifest_sha = manifest.get("manifest_sha256", "")

    release = {
        "release_id": release_id,
        "version": version,
        "scope": scope,
        "compile_id": compile_id,
        "release_manifest": {
            "release_id": release_id,
            "release_version": version,
            "compile_id": compile_id,
            "compiler_version": COMPILER_VERSION,
            "created_at": now,
            "release_owner": release_owner,
            "scope": scope,
            "task_id": task_id,
            "strategy_script_group_id": manifest.get("strategy_script_group_id"),
            "baseline_id": manifest.get("baseline_id"),
            "baseline_version": manifest.get("baseline_version"),
            "baseline_content_hash": manifest.get("baseline_content_hash"),
            "prompt_sha256": prompt_sha,
            "execution_prompt_sha256": manifest.get("execution_prompt_sha256"),
            "route_table_sha256": manifest.get("route_table_sha256"),
            "manifest_sha256": manifest_sha,
            "regression_status": "not_tested",
            "regression_gap": True,
            "regression_gap_reason": "MVP: 暂无历史 case",
        },
        "execution_prompt": execution_prompt,
        "route_table": route_table,
        "strategy_prompt": compilation.get("strategy_prompt", ""),
        "script_prompt": compilation.get("script_prompt", ""),
        "combined_prompt": compilation.get("combined_prompt", ""),
        "verification_card": {
            "release_id": release_id,
            "release_version": version,
            "prompt_sha256": prompt_sha,
            "execution_prompt_sha256": manifest.get("execution_prompt_sha256"),
            "route_table_sha256": manifest.get("route_table_sha256"),
            "scope": scope,
            "verification_steps": [
                "1. 核对发布包 ID 和版本",
                "2. 复制正式 Prompt",
                "3. 用收到的 Prompt 重新计算 SHA-256",
                "4. 与核验卡中的 prompt_sha256 比对",
                "5. 一致则标记 integrity_verified",
            ],
            "note": "不得手改 Prompt；哈希不匹配则视为外部非受控派生物",
        },
        "status": "published",
        "created_at": now,
    }

    db["releases"].append(release)
    _save_db(db)
    
    # 记录审计
    audit_event(
        actor=release_owner,
        role="release_owner",
        scope="release",
        obj_id=release_id,
        action="create_release",
        result="published",
        reason="发布评价规则发布包",
        data_level="D1",
    )

    return release


def get_release(release_id):
    db = _load_db()
    for r in db.get("releases", []):
        if r["release_id"] == release_id:
            return r
    return None


def list_releases(task_id=None):
    db = _load_db()
    result = []
    for r in db.get("releases", []):
        if task_id and r.get("release_manifest", {}).get("task_id") != task_id:
            continue
        result.append({
            "release_id": r["release_id"],
            "version": r.get("version"),
            "scope": r.get("scope"),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
            "prompt_sha256": r.get("release_manifest", {}).get("prompt_sha256", ""),
        })
    return result


# ── E-06: 人工交付记录 ────────────────────────────
def record_delivery(release_id, deliverer="admin", recipient="",
                    method="manual_copy", integrity_verified=False,
                    note=""):
    """E-06: 记录交付"""
    db = _load_db()
    if "deliveries" not in db:
        db["deliveries"] = []

    release = get_release(release_id)
    if not release:
        return {"error": "release_not_found"}

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    delivery_id = _new_id("dlv")

    # 核验状态
    if integrity_verified:
        verify_status = "integrity_verified"
    else:
        verify_status = "sent_unverified"

    delivery = {
        "delivery_id": delivery_id,
        "release_id": release_id,
        "release_version": release.get("version"),
        "deliverer": deliverer,
        "recipient": recipient,
        "method": method,
        "prompt_sha256": release.get("release_manifest", {}).get("prompt_sha256", ""),
        "execution_prompt_sha256": release.get("release_manifest", {}).get("execution_prompt_sha256", ""),
        "route_table_sha256": release.get("release_manifest", {}).get("route_table_sha256", ""),
        "verify_status": verify_status,
        "note": note,
        "delivered_at": now,
    }

    db["deliveries"].append(delivery)
    _save_db(db)

    # 审计
    audit_event(
        actor=deliverer,
        role="release_owner",
        scope="delivery",
        obj_id=delivery_id,
        action="deliver",
        result=verify_status,
        reason="交付发布包给 {}".format(recipient or "未指定"),
        data_level="D1",
    )

    return delivery


def list_deliveries(release_id=None):
    db = _load_db()
    result = db.get("deliveries", [])
    if release_id:
        result = [d for d in result if d.get("release_id") == release_id]
    return result



# ── HTTP 服务 ─────────────────────────────────────
ALLOWED_ORIGINS = {
    item.strip() for item in os.environ.get(
        "AI_CALL_EVAL_ALLOWED_ORIGINS",
        "http://127.0.0.1:8897,http://localhost:8897",
    ).split(",") if item.strip()
}


def _result_status(result, success=200):
    error = result.get("error") if isinstance(result, dict) else None
    if not error:
        return success
    if error in ("source_not_found", "task_not_found", "object_not_found", "compile_not_found",
                 "graph_not_found", "graph_node_not_found", "graph_edge_not_found",
                 "script_variant_not_found"):
        return 404
    if error in ("llm_parse_failed", "llm_json_error"):
        return 422
    if error in ("llm_call_failed",):
        return 502
    if error in ("llm_client_not_available", "api_key_not_set"):
        return 503
    if error in ("baseline_hash_mismatch", "gate_required", "invalid_target_status",
                 "approved_graph_immutable", "approved_variant_immutable", "script_variant_in_use",
                 "route_table_drift", "layout_stale"):
        return 409
    return 400


class Handler(BaseHTTPRequestHandler):
    def _cors_origin(self):
        origin = self.headers.get("Origin")
        return origin if origin in ALLOWED_ORIGINS else None

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        allowed_origin = self._cors_origin()
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return {"__body_error__": "invalid_content_length"}
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8-sig"))
            return body if isinstance(body, dict) else {"__body_error__": "json_object_required"}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"__body_error__": "invalid_json", "message": str(exc)}

    def do_OPTIONS(self):
        allowed_origin = self._cors_origin()
        if not allowed_origin:
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/health":
            return self._json(200, {"status": "ok", "stage": "B"})

        if path == "/api/tasks":
            return self._json(200, {"tasks": list_tasks()})

        if path == "/api/sources":
            task_id = qs.get("task_id", [None])[0]
            return self._json(200, {"sources": list_sources_safe(task_id)})

        if path == "/api/utterances":
            source_id = qs.get("source_id", [None])[0]
            # D3 原始正文不通过默认查询接口返回；LLM/解析流程在服务内部读取。
            safe_utterances = []
            for item in list_utterances(source_id):
                safe = dict(item)
                safe.pop("content", None)
                safe["content_redacted"] = True
                safe_utterances.append(safe)
            return self._json(200, {"utterances": safe_utterances})

        if path == "/api/sessions":
            source_id = qs.get("source_id", [None])[0]
            return self._json(200, {"sessions": list_sessions(source_id)})

        if path == "/api/evidence":
            task_id = qs.get("task_id", [None])[0]
            source_id = qs.get("source_id", [None])[0]
            return self._json(200, {"evidence": list_evidence(task_id, source_id)})

        if path == "/api/input-files":
            return self._json(200, {"files": list_input_files()})

        m = re.match(r"^/api/source/(\w+)/snapshot$", path)
        if m:
            source_id = m.group(1)
            src = get_source_snapshot(source_id)
            if not src:
                return self._json(404, {"error": "source_not_found"})
            safe_src = dict(src)
            snapshot_available = bool(safe_src.pop("snapshot", None))
            safe_src["snapshot_available"] = snapshot_available
            safe_src["snapshot_redacted"] = True
            return self._json(200, safe_src)

        if path == "/api/knowledge":
            task_id = qs.get("task_id", [None])[0]
            status = qs.get("status", [None])[0]
            obj_type = qs.get("type", [None])[0]
            source_id = qs.get("source_id", [None])[0]
            include_archived = qs.get("include_archived", ["0"])[0] == "1"
            # The normal view is the deduplicated candidate set. The complete
            # historical set is opt-in so review screens do not mix rerun
            # artifacts into the active queue.
            all_knowledge = list_knowledge(task_id, source_id, status, obj_type, True)
            visible_knowledge = list_knowledge(task_id, source_id, status, obj_type, False)
            knowledge = all_knowledge if include_archived else visible_knowledge
            explicit_archived = sum(
                1 for o in all_knowledge
                if o.get("status") == "archived" and o.get("duplicate_of")
            )
            # Older objects predate dedupe metadata. Count entries hidden by
            # the query-time fingerprint pass as archived duplicates too.
            active_count = sum(1 for o in all_knowledge if o.get("status") != "archived")
            dynamic_hidden = max(0, active_count - len(visible_knowledge)) if not status else 0
            archived_duplicates = explicit_archived + dynamic_hidden
            possible_duplicates = sum(1 for o in all_knowledge if o.get("possible_duplicate_of"))
            return self._json(200, {"knowledge": knowledge, "archived_duplicates": archived_duplicates,
                                    "possible_duplicates": possible_duplicates})

        if path == "/api/script-variants":
            result = get_node_script_workspace(
                qs.get("task_id", [""])[0],
                qs.get("graph_id", [""])[0],
                qs.get("node_id", [""])[0],
            )
            return self._json(_result_status(result), result)

        if path == "/api/node-content":
            result = get_node_content_workspace(
                qs.get("task_id", [""])[0],
                qs.get("graph_id", [""])[0],
                qs.get("node_origin", [""])[0],
                qs.get("node_id", [""])[0],
            )
            return self._json(_result_status(result), result)

        if path == "/api/edge-condition":
            result = get_edge_condition_workspace(
                qs.get("task_id", [""])[0],
                qs.get("graph_id", [""])[0],
                qs.get("edge_origin", [""])[0],
                qs.get("edge_id", [""])[0],
            )
            return self._json(_result_status(result), result)

        if path == "/api/graph-baselines":
            task_id = qs.get("task_id", [None])[0]
            source_id = qs.get("source_id", [None])[0]
            return self._json(200, {"baselines": list_graph_baselines(task_id, source_id)})

        if path == "/api/graph-export":
            result = export_graph_document(
                qs.get("task_id", [""])[0],
                qs.get("graph_id", [""])[0],
            )
            return self._json(_result_status(result), result)

        if path == "/api/graph-layout":
            result = get_graph_layout(
                qs.get("task_id", [""])[0], qs.get("graph_id", [""])[0]
            )
            return self._json(_result_status(result), result)

        if path == "/api/knowledge/" + (qs.get("id", [""])[0]):
            pass

        # /api/knowledge/<id>
        m = re.match(r"^/api/knowledge/(\w+)$", path)
        if m:
            obj = get_knowledge_object(m.group(1))
            if not obj:
                return self._json(404, {"error": "object_not_found"})
            return self._json(200, obj)

        # /api/knowledge/<id>/diff
        m = re.match(r"^/api/knowledge/(\w+)/diff$", path)
        if m:
            result = diff_against_baseline(m.group(1))
            if "error" in result:
                return self._json(404, result)
            return self._json(200, result)

        if path == "/api/changes":
            task_id = qs.get("task_id", [None])[0]
            status = qs.get("status", [None])[0]
            change_type = qs.get("change_type", [None])[0]
            return self._json(200, {"changes": list_changes(task_id, status, change_type)})

        # /api/changes/<id>
        m = re.match(r"^/api/changes/(\w+)$", path)
        if m:
            ch = get_change(m.group(1))
            if not ch:
                return self._json(404, {"error": "change_not_found"})
            return self._json(200, ch)


        if path == "/api/llm-config":
            if not llm_client:
                return self._json(500, {"error": "llm_client_not_available"})
            return self._json(200, llm_client.get_safe_config())

        # /api/llm-config/test - test LLM call
        if path == "/api/llm-config/test":
            if not llm_client:
                return self._json(500, {"error": "llm_client_not_available"})
            result = llm_client.chat(
                [{"role": "user", "content": "请只回复：连接正常"}],
                max_tokens=128,
                thinking={"type": "disabled"},
            )
            return self._json(200, result)


        if path == "/api/gates":
            task_id = qs.get("task_id", [None])[0]
            gate_id = qs.get("gate_id", [None])[0]
            target_id = qs.get("target_object_id", [None])[0]
            return self._json(200, {"gates": list_gates(task_id, gate_id, target_id)})

        if path == "/api/audit":
            actor = qs.get("actor", [None])[0]
            action = qs.get("action", [None])[0]
            obj_id = qs.get("object_id", [None])[0]
            return self._json(200, {"events": list_audit_events(actor, action, obj_id)})


        if path == "/api/compile-input":
            task_id = qs.get("task_id", [None])[0]
            source_id = qs.get("source_id", [None])[0]
            scope = qs.get("scope", ["general"])[0]
            group_id = qs.get("strategy_script_group_id", [None])[0]
            result = collect_compile_input(source_id, task_id, scope, group_id)
            return self._json(_result_status(result), result)


        if path == "/api/compilations":
            task_id = qs.get("task_id", [None])[0]
            return self._json(200, {"compilations": list_compilations(task_id)})

        # /api/compilation/<id>
        m = re.match(r"^/api/compilation/(\w+)$", path)
        if m:
            c = get_compilation(m.group(1))
            if not c:
                return self._json(404, {"error": "compile_not_found"})
            return self._json(200, c)

        if path == "/api/releases":
            task_id = qs.get("task_id", [None])[0]
            return self._json(200, {"releases": list_releases(task_id)})

        # /api/release/<id>
        m = re.match(r"^/api/release/(\w+)$", path)
        if m:
            r = get_release(m.group(1))
            if not r:
                return self._json(404, {"error": "release_not_found"})
            return self._json(200, r)

        if path == "/api/deliveries":
            release_id = qs.get("release_id", [None])[0]
            return self._json(200, {"deliveries": list_deliveries(release_id)})

        return self._json(404, {"error": "not_found", "path": path})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        if body.get("__body_error__"):
            return self._json(400, {"error": body["__body_error__"], "message": body.get("message", "")})

        if path == "/api/graph-baselines/parse":
            result = parse_graph_import_bundle(body.get("content", ""), body.get("filename", ""))
            return self._json(200 if "error" not in result else 400, result)

        if path == "/api/graph-layout/analyze":
            result = analyze_graph_layout(
                body.get("task_id", ""), body.get("graph_id", ""), body.get("reviewer", "system")
            )
            return self._json(_result_status(result), result)

        if path == "/api/graph-layout":
            result = save_graph_layout(
                body.get("task_id", ""), body.get("graph_id", ""),
                body.get("materialized_graph_hash", ""), body.get("node_updates", []),
                body.get("edge_updates", []), body.get("editor", "admin"),
            )
            return self._json(_result_status(result), result)

        if path == "/api/node-content":
            result = save_node_content(
                body.get("task_id", ""), body.get("graph_id", ""),
                body.get("node_origin", ""), body.get("node_id", ""),
                body.get("content"), body.get("reviewer", "admin"),
            )
            return self._json(_result_status(result), result)

        if path == "/api/graph-baselines/match":
            result = confirm_graph_match(
                task_id=body.get("task_id", ""), baseline_id=body.get("baseline_id", ""),
                candidate_id=body.get("candidate_id", ""), reference_id=body.get("reference_id", ""),
                reviewer=body.get("reviewer", "admin"), decision=body.get("decision", "confirmed"),
                reason=body.get("reason", ""),
            )
            return self._json(200 if "error" not in result else 400, result)

        if path == "/api/graph-review":
            result = review_graph_candidate(
                task_id=body.get("task_id", ""),
                graph_id=body.get("graph_id", ""),
                reviewer=body.get("reviewer", "admin"),
                decision=body.get("decision", "approved"),
                reason=body.get("reason", ""),
            )
            return self._json(200 if "error" not in result else 400, result)

        if path == "/api/graph-baselines":
            result = create_graph_baseline(
                task_id=body.get("task_id", ""),
                source_id=body.get("source_id"),
                name=body.get("name", "未命名基线 Graph"),
                document=body.get("graph") or body.get("document") or {},
                origin=body.get("origin", "manual"),
                source_filename=body.get("source_filename"),
                layout_profile=body.get("layout_profile"),
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(201, result)

        if path == "/api/script-documents/parse":
            task_id = body.get("task_id", "")
            document_text = body.get("content", "")
            document_filename = body.get("filename", "")
            if not task_id:
                return self._json(400, {"error": "missing task_id"})
            if not document_text:
                return self._json(400, {"error": "missing content"})
            result = llm_map_script_documents(
                task_id, document_text, document_filename, body.get("baseline_id")
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        if path == "/api/import":
            filename = body.get("filename", "")
            if not filename:
                return self._json(400, {"error": "missing filename"})
            filepath = os.path.join(INPUT_DIR, filename)
            if not os.path.exists(filepath):
                return self._json(404, {"error": "file_not_found",
                                       "path": filepath,
                                       "input_dir": INPUT_DIR})
            result = import_txt(filepath, filename)
            if "error" in result and result["error"] == "duplicate":
                return self._json(409, result)
            return self._json(200, result)

        if path == "/api/import-content":
            filename = body.get("filename", "")
            content = body.get("content")
            if content is None:
                return self._json(400, {"error": "missing content"})
            if not isinstance(content, str):
                return self._json(400, {"error": "invalid_content"})
            if len(content.encode("utf-8")) > 10 * 1024 * 1024:
                return self._json(413, {"error": "content_too_large", "max_bytes": 10 * 1024 * 1024})
            try:
                result = import_text_content(filename, content)
            except Exception:
                # Keep client requests actionable if persistence/parsing fails, and
                # retain the full traceback in the server log for diagnosis.
                traceback.print_exc(file=sys.stderr)
                return self._json(500, {"error": "import_failed", "message": "TXT 导入失败，请检查后端日志"})
            if result.get("error") == "duplicate":
                return self._json(409, result)
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        if path == "/api/tasks/rerun":
            result = rerun_task(body.get("task_id", ""))
            if "error" in result:
                code = 404 if result["error"] in ("task_not_found", "source_not_found") else 409
                return self._json(code, result)
            return self._json(201, result)

        if path == "/api/parse":
            source_id = body.get("source_id", "")
            if not source_id:
                return self._json(400, {"error": "missing source_id"})
            result = parse_source(source_id)
            if "error" in result:
                code = 409 if result["error"] == "already_parsed" else 404
                return self._json(code, result)
            return self._json(200, result)

        
        if path == "/api/knowledge":
            new_obj = create_knowledge_object(
                task_id=body.get("task_id", ""),
                source_id=body.get("source_id", ""),
                obj_type=body.get("type", "strategy_node"),
                content=body.get("content", ""),
                evidence_refs=body.get("evidence_refs", []),
                scope=body.get("scope", "general"),
                parent_version=body.get("parent_version"),
                linkage=body.get("linkage"),
                conflict_set=body.get("conflict_set"),
            )
            if "error" in new_obj:
                return self._json(400, new_obj)
            return self._json(200, new_obj)

        if path == "/api/script-variants":
            result = create_script_variant(
                body.get("task_id", ""), body.get("graph_id", ""), body.get("node_id", ""),
                body.get("evidence_id", ""), body.get("content", ""), body.get("editor", "admin"),
            )
            return self._json(_result_status(result, 201), result)

        if path == "/api/script-selections":
            result = save_node_script_selections(
                body.get("task_id", ""), body.get("graph_id", ""), body.get("node_id", ""),
                body.get("selections", []), body.get("editor", "admin"),
            )
            return self._json(_result_status(result), result)

        if path == "/api/script-variants/delete":
            result = delete_script_variant(
                body.get("task_id", ""), body.get("variant_id", ""), body.get("editor", "admin"),
            )
            return self._json(_result_status(result), result)

        if path == "/api/edge-condition":
            result = save_edge_condition(
                body.get("task_id", ""), body.get("graph_id", ""),
                body.get("edge_origin", ""), body.get("edge_id", ""),
                body.get("condition", ""), body.get("reviewer", "admin"),
            )
            return self._json(_result_status(result), result)

        if path == "/api/evidence/review":
            result = review_evidence(
                task_id=body.get("task_id", ""),
                evidence_id=body.get("evidence_id", ""),
                reviewer=body.get("reviewer", ""),
                decision=body.get("decision", "pending"),
                evidence_kind=body.get("evidence_kind"),
                conflict_set=body.get("conflict_set"),
                reason=body.get("reason", ""),
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        # /api/knowledge/<id>/new-version
        m = re.match(r"^/api/knowledge/(\w+)/new-version$", path)
        if m:
            result = new_version(m.group(1), body.get("content", ""),
                                  body.get("evidence_refs"), body.get("reason", ""))
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        if path == "/api/changes":
            result = create_change_proposal(
                task_id=body.get("task_id", ""),
                change_type=body.get("change_type", "add"),
                target_object_id=body.get("target_object_id"),
                new_object=body.get("new_object"),
                baseline_id=body.get("baseline_id"),
                reason=body.get("reason", ""),
                evidence_refs=body.get("evidence_refs", []),
                scope=body.get("scope", "general"),
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)


        if path == "/api/llm-config":
            if not llm_client:
                return self._json(500, {"error": "llm_client_not_available"})
            config = llm_client.load_config()
            if "base_url" in body:
                config["base_url"] = body["base_url"]
            if "model" in body:
                config["model"] = body["model"]
            if "temperature" in body:
                config["temperature"] = body["temperature"]
            if "max_tokens" in body:
                config["max_tokens"] = body["max_tokens"]
            if "max_utterances_per_call" in body:
                config["max_utterances_per_call"] = body["max_utterances_per_call"]
            if "api_key" in body and body["api_key"]:
                llm_client.set_runtime_api_key(body["api_key"])
            llm_client.save_config(config)
            changed_fields = [key for key in body.keys() if key != "api_key"]
            if body.get("api_key"):
                changed_fields.append("api_key_updated")
            audit = audit_event(
                actor="admin",
                role="product_ops",
                scope="llm_config",
                obj_id="llm_config",
                action="update_llm_config",
                result="saved",
                reason="updated fields: " + ", ".join(changed_fields),
                data_level="D3",
            )
            safe_config = llm_client.get_safe_config()
            safe_config["audit_id"] = audit["audit_id"]
            return self._json(200, safe_config)

        
        if path == "/api/llm-extract-evidence":
            source_id = body.get("source_id", "")
            max_utts = body.get("max_utts", 20)
            if not source_id:
                return self._json(400, {"error": "missing source_id"})
            result = llm_extract_evidence(source_id, max_utts)
            return self._json(_result_status(result), result)


        if path == "/api/llm-extract-strategy":
            source_id = body.get("source_id", "")
            if not source_id:
                return self._json(400, {"error": "missing source_id"})
            result = llm_extract_strategy(
                source_id,
                body.get("max_utts", 20),
                include_all=body.get("include_all") is True,
            )
            return self._json(_result_status(result), result)

        if path == "/api/llm-map-scripts":
            source_id = body.get("source_id", "")
            if not source_id:
                return self._json(400, {"error": "missing source_id"})
            result = llm_map_scripts(source_id, body.get("max_utts", 15))
            return self._json(_result_status(result), result)

        if path == "/api/gate":
            result = gate_action(
                gate_id=body.get("gate_id", ""),
                task_id=body.get("task_id", ""),
                reviewer=body.get("reviewer", ""),
                decision=body.get("decision", ""),
                reason=body.get("reason", ""),
                target_object_id=body.get("target_object_id"),
                evidence_refs=body.get("evidence_refs"),
                before_obj=body.get("before_object"),
                after_obj=body.get("after_object"),
                target_expert=body.get("target_expert"),
                baseline_id=body.get("baseline_id"),
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        if path == "/api/audit":
            result = audit_event(
                actor=body.get("actor", ""),
                role=body.get("role", ""),
                scope=body.get("scope", ""),
                obj_id=body.get("object_id", ""),
                action=body.get("action", ""),
                result=body.get("result", ""),
                reason=body.get("reason", ""),
                data_level=body.get("data_level", "D2"),
            )
            return self._json(200, result)


        if path == "/api/llm-generate-strategy-prompt":
            result = llm_generate_strategy_prompt(
                source_id=body.get("source_id"),
                task_id=body.get("task_id"),
                scope=body.get("scope", "general"),
                strategy_script_group_id=body.get("strategy_script_group_id"),
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        if path == "/api/generate-execution-prompt":
            result = generate_execution_prompt(
                source_id=body.get("source_id"),
                task_id=body.get("task_id"),
                scope=body.get("scope", "general"),
                strategy_script_group_id=body.get("strategy_script_group_id"),
            )
            return self._json(_result_status(result), result)

        if path == "/api/llm-generate-script-prompt":
            result = llm_generate_script_prompt(
                source_id=body.get("source_id"),
                task_id=body.get("task_id"),
                scope=body.get("scope", "general"),
                strategy_script_group_id=body.get("strategy_script_group_id"),
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        if path == "/api/compile":
            result = compile_release(
                source_id=body.get("source_id"),
                task_id=body.get("task_id"),
                scope=body.get("scope", "general"),
                prompt_size_budget=body.get("prompt_size_budget", 262144),
                strategy_script_group_id=body.get("strategy_script_group_id"),
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        if path == "/api/release":
            result = create_release_package(
                compile_id=body.get("compile_id", ""),
                release_owner=body.get("release_owner", "admin"),
                scope=body.get("scope", "general"),
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        if path == "/api/delivery":
            result = record_delivery(
                release_id=body.get("release_id", ""),
                deliverer=body.get("deliverer", "admin"),
                recipient=body.get("recipient", ""),
                method=body.get("method", "manual_copy"),
                integrity_verified=body.get("integrity_verified", False),
                note=body.get("note", ""),
            )
            if "error" in result:
                return self._json(400, result)
            return self._json(200, result)

        return self._json(404, {"error": "not_found", "path": path})

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main():
    port = 8898
    print(f"AI 电话评价 Agent 后端: http://127.0.0.1:{port}/")
    print(f"输入文档目录: {INPUT_DIR}")
    # Long, high-quality LLM calls must not freeze health checks or the rest of the UI.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
