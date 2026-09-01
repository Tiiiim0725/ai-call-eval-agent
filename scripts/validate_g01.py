"""Read-only G-01 continuity check for the running MVP services."""

from __future__ import annotations

import hashlib
import json
import sys
from urllib.request import urlopen


API = "http://127.0.0.1:8898/api"


def get(path: str) -> dict:
    with urlopen(API + path, timeout=5) as response:
        return json.load(response)


def main() -> int:
    tasks = get("/tasks").get("tasks", [])
    evidence = get("/evidence").get("evidence", [])
    knowledge = get("/knowledge").get("knowledge", [])
    gates = get("/gates").get("gates", [])
    compilations = get("/compilations").get("compilations", [])
    releases = get("/releases").get("releases", [])
    deliveries = get("/deliveries").get("deliveries", [])
    audit = get("/audit").get("events", [])

    if not tasks or not evidence or not knowledge or not gates:
        raise AssertionError("source/evidence/knowledge/gate chain is incomplete")
    approved = [item for item in knowledge if item.get("status") == "approved"]
    if not approved:
        raise AssertionError("no approved knowledge object")
    evidence_ids = {item.get("evidence_id") for item in evidence}
    utterance_to_evidence = {
        item.get("utterance_id"): item.get("evidence_id")
        for item in evidence
        if item.get("utterance_id") and item.get("evidence_id")
    }
    missing_refs = [
        item["object_id"]
        for item in approved
        if not all(ref in evidence_ids or ref in utterance_to_evidence for ref in item.get("evidence_refs", []))
    ]
    if missing_refs:
        raise AssertionError("approved objects with missing evidence refs: " + ", ".join(missing_refs))
    approved_gate_ids = {item.get("target_object_id") for item in gates if item.get("decision") == "approved"}
    if not {item["object_id"] for item in approved}.issubset(approved_gate_ids):
        raise AssertionError("approved knowledge is missing an approved Gate")
    if not compilations or not releases or not deliveries:
        raise AssertionError("compile/release/delivery chain is incomplete")

    compile_summary = compilations[-1]
    compile_detail = get("/compilation/" + compile_summary["compile_id"])
    manifest = compile_detail.get("manifest", {})
    input_ids = manifest.get("input_objects", [])
    if isinstance(input_ids, str):
        input_ids = input_ids.split()
    approved_ids = {item["object_id"] for item in approved}
    if not set(input_ids).issubset(approved_ids):
        raise AssertionError("compile input contains a non-approved object")
    combined_prompt = compile_detail.get("combined_prompt", "")
    if not combined_prompt:
        raise AssertionError("compile detail has no combined Prompt")
    compile_hash = hashlib.sha256(combined_prompt.encode("utf-8")).hexdigest()
    if compile_hash != manifest.get("combined_prompt_sha256"):
        raise AssertionError("compile Prompt SHA-256 mismatch")

    release_summary = releases[-1]
    release_detail = get("/release/" + release_summary["release_id"])
    release_manifest = release_detail.get("release_manifest", {})
    if release_manifest.get("prompt_sha256") != hashlib.sha256(
        release_detail.get("combined_prompt", "").encode("utf-8")
    ).hexdigest():
        raise AssertionError("release Prompt SHA-256 mismatch")
    delivery = [item for item in deliveries if item.get("release_id") == release_summary["release_id"]]
    if not any(item.get("verify_status") == "integrity_verified" for item in delivery):
        raise AssertionError("release has no integrity_verified delivery")
    if not any(item.get("immutable") is True for item in audit):
        raise AssertionError("audit chain has no immutable event")

    result = {
        "status": "PASS",
        "task_id": tasks[-1]["task_id"],
        "source_id": tasks[-1].get("source_id"),
        "counts": {
            "tasks": len(tasks),
            "evidence": len(evidence),
            "knowledge": len(knowledge),
            "approved_knowledge": len(approved),
            "direct_evidence_refs": sum(
                ref in evidence_ids
                for item in approved
                for ref in item.get("evidence_refs", [])
            ),
            "legacy_utterance_refs": sum(
                ref in utterance_to_evidence
                for item in approved
                for ref in item.get("evidence_refs", [])
            ),
            "approved_gates": len(approved_gate_ids),
            "compilations": len(compilations),
            "releases": len(releases),
            "deliveries": len(deliveries),
            "audit_events": len(audit),
        },
        "compile_id": compile_summary["compile_id"],
        "release_id": release_summary["release_id"],
        "verified_delivery_id": next(item["delivery_id"] for item in delivery if item.get("verify_status") == "integrity_verified"),
        "prompt_sha256": release_manifest.get("prompt_sha256"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI diagnostic
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
