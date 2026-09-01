"""Read-only/guarded G-02 safety checks for the MVP HTTP API."""

from __future__ import annotations

import json
import pathlib
from urllib.error import HTTPError
from urllib.request import Request, urlopen


API = "http://127.0.0.1:8898/api"
INPUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "input-docs"


def get(path: str) -> dict:
    with urlopen(API + path, timeout=5) as response:
        return json.load(response)


def post(path: str, body: dict) -> tuple[int, dict]:
    request = Request(
        API + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def main() -> int:
    tasks = get("/tasks").get("tasks", [])
    if not tasks:
        raise AssertionError("no demo task")
    task = next((item for item in tasks if (INPUT_DIR / item.get("filename", "")).is_file()), None)
    if not task:
        raise AssertionError("no demo task has a matching local input file")

    duplicate_status, duplicate = post("/import", {"filename": task["filename"]})
    if duplicate_status != 409 or duplicate.get("error") != "duplicate":
        raise AssertionError(f"duplicate import did not block: {duplicate_status} {duplicate}")

    parse_status, parse_result = post("/parse", {"source_id": task["source_id"]})
    if parse_status != 409 or parse_result.get("error") != "already_parsed":
        raise AssertionError(f"reparse did not preserve existing parse: {parse_status} {parse_result}")

    unknown_status, unknown = post("/parse", {"source_id": "src_missing_g02"})
    if unknown_status != 404 or unknown.get("error") != "source_not_found":
        raise AssertionError(f"unknown source did not block: {unknown_status} {unknown}")

    change_status, change = post(
        "/changes",
        {
            "task_id": task["task_id"],
            "change_type": "add",
            "new_object": {"content": "should not be saved"},
            "evidence_refs": [],
        },
    )
    if change_status != 400 or change.get("error") != "missing_evidence":
        raise AssertionError(f"evidence-less change did not block: {change_status} {change}")

    snapshot = get("/source/" + task["source_id"] + "/snapshot")
    if "snapshot" in snapshot or snapshot.get("snapshot_redacted") is not True:
        raise AssertionError("D3 source snapshot was exposed")
    utterances = get("/utterances?source_id=" + task["source_id"])
    if any("content" in item for item in utterances.get("utterances", [])):
        raise AssertionError("D3 utterance content was exposed")

    compile_input = get("/compile-input?task_id=" + task["task_id"])
    blocked = compile_input.get("blocked", [])
    if not blocked:
        raise AssertionError("approved-only compiler returned no blocked candidates")
    if any(item.get("status") != "approved" for item in compile_input.get("objects", [])):
        raise AssertionError("compile input contains a non-approved object")

    deliveries = get("/deliveries").get("deliveries", [])
    if not any(item.get("verify_status") == "sent_unverified" for item in deliveries):
        raise AssertionError("hash-mismatch downgrade is not represented")
    knowledge = get("/knowledge").get("knowledge", [])
    conflict_count = sum(bool(item.get("conflict_set")) for item in knowledge)
    audit = get("/audit").get("events", [])

    result = {
        "status": "PASS",
        "checks": {
            "duplicate_import": "blocked_409",
            "reparse": "preserved_409",
            "unknown_source": "blocked_404",
            "missing_evidence_change": "blocked_400",
            "d3_snapshot": "redacted",
            "d3_utterances": "content_redacted",
            "approved_only_blocked_candidates": len(blocked),
            "hash_mismatch_downgrade": "sent_unverified_present",
            "conflict_sets_present": conflict_count,
            "immutable_audit_events": sum(item.get("immutable") is True for item in audit),
        },
        "note": "超预算编译和真实 LLM 失败重试未在本次安全验收中触发，以避免额外模型调用；代码路径保留显式阻断。",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
