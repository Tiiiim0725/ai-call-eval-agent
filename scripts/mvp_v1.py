"""Thin vertical slice for the AI phone-evaluation knowledge workflow.

This is an experimental MVP v1 prototype. It preserves source evidence and
requires an explicit target-speaker choice and approval step; it does not
pretend to infer a finished evaluation policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PARSER_VERSION = "mvp-v1-parser-0.1"
UTTERANCE_RE = re.compile(
    r"^\s*(?P<speaker>[^:\uff1a\t]+?)\s*(?:\[(?P<timestamp>[^\]]+)\])?\s*[:\uff1a]\s*(?P<text>.*?)\s*$"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_source(raw: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    utterances: list[dict[str, Any]] = []
    unparsed: list[dict[str, Any]] = []
    byte_cursor = 0

    for line_no, line_bytes in enumerate(raw.splitlines(keepends=True), start=1):
        line_text = line_bytes.decode("utf-8", errors="replace")
        content = line_text.rstrip("\r\n")
        match = UTTERANCE_RE.match(content)
        if not match:
            if content.strip():
                unparsed.append({"line_no": line_no, "text": content})
            byte_cursor += len(line_bytes)
            continue

        speaker = match.group("speaker").strip()
        text = match.group("text").strip()
        text_bytes = text.encode("utf-8")
        content_offset = line_bytes.find(text_bytes)
        if content_offset < 0:
            content_offset = 0

        utterance_id = f"utt_{len(utterances) + 1:04d}"
        utterances.append(
            {
                "utterance_id": utterance_id,
                "line_no": line_no,
                "speaker_label": speaker,
                "timestamp": match.group("timestamp"),
                "text": text,
                "byte_start": byte_cursor,
                "byte_end": byte_cursor + len(line_bytes),
                "text_byte_start": byte_cursor + content_offset,
                "text_byte_end": byte_cursor + content_offset + len(text_bytes),
                "text_sha256": hashlib.sha256(text_bytes).hexdigest(),
            }
        )
        byte_cursor += len(line_bytes)

    return utterances, unparsed


def ingest(input_path: Path, out_dir: Path, target_speaker: str | None) -> Path:
    raw = input_path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    source_id = f"src_{source_hash[:16]}"
    task_id = f"task_{short_hash((source_hash + PARSER_VERSION).encode())}"
    task_dir = out_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "source").mkdir(exist_ok=True)

    source_path = task_dir / "source" / f"{source_id}.txt"
    source_path.write_bytes(raw)
    utterances, unparsed = parse_source(raw)
    speakers = sorted({item["speaker_label"] for item in utterances})

    metadata = {
        "task_id": task_id,
        "status": "awaiting_target_expert" if not target_speaker else "candidate_pending_review",
        "created_at": now(),
        "parser_version": PARSER_VERSION,
        "source": {
            "source_id": source_id,
            "sha256": source_hash,
            "byte_length": len(raw),
            "encoding": "utf-8",
            "original_filename": input_path.name,
        },
        "target_speaker": target_speaker,
        "speakers": speakers,
        "unparsed_line_count": len(unparsed),
    }
    write_json(task_dir / "task.json", metadata)
    write_json(task_dir / "utterances.json", utterances)
    write_json(task_dir / "unparsed.json", unparsed)

    evidence: list[dict[str, Any]] = []
    if target_speaker:
        for utterance in utterances:
            if utterance["speaker_label"] != target_speaker:
                continue
            evidence_id = (
                f"evd_{source_id}_{utterance['utterance_id']}"
                f"_{utterance['text_byte_start']}_{utterance['text_byte_end']}"
            )
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "source_id": source_id,
                    "utterance_id": utterance["utterance_id"],
                    "speaker_label": utterance["speaker_label"],
                    "timestamp": utterance["timestamp"],
                    "byte_start": utterance["text_byte_start"],
                    "byte_end": utterance["text_byte_end"],
                    "text": utterance["text"],
                    "evidence_kind": "unclassified",
                    "review_status": "candidate",
                }
            )
    write_json(task_dir / "evidence.json", evidence)

    candidates = [
        {
            "candidate_id": f"cand_{short_hash(item['evidence_id'].encode())}",
            "kind": "unclassified_knowledge",
            "status": "candidate",
            "claim": item["text"],
            "evidence_refs": [item["evidence_id"]],
            "applicability_scope": "scope_pending",
            "note": "Prototype keeps the claim unclassified until human review.",
        }
        for item in evidence
    ]
    write_json(task_dir / "candidates.json", candidates)
    print(json.dumps({"task_id": task_id, "task_dir": str(task_dir), "status": metadata["status"], "evidence_count": len(evidence)}, ensure_ascii=False))
    return task_dir


def approve(task_dir: Path, reviewer: str) -> None:
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    candidates = json.loads((task_dir / "candidates.json").read_text(encoding="utf-8"))
    evidence = json.loads((task_dir / "evidence.json").read_text(encoding="utf-8"))
    if not task.get("target_speaker"):
        raise SystemExit("Cannot approve before a target speaker is selected.")
    if not candidates:
        raise SystemExit("No candidate evidence is available for approval.")

    approved_at = now()
    for candidate in candidates:
        candidate["status"] = "approved"
        candidate["approved_by"] = reviewer
        candidate["approved_at"] = approved_at
    for item in evidence:
        item["review_status"] = "approved"

    knowledge_version = "knowledge-v1"
    package_id = f"pkg_{task['source']['source_id']}_{knowledge_version}"
    manifest = {
        "package_id": package_id,
        "package_version": "1.0.0-prototype",
        "task_id": task["task_id"],
        "source_sha256": task["source"]["sha256"],
        "knowledge_version": knowledge_version,
        "included_candidate_ids": [item["candidate_id"] for item in candidates],
        "reviewer": reviewer,
        "approved_at": approved_at,
        "prototype_only": True,
    }
    release_dir = task_dir / "release"
    release_dir.mkdir(exist_ok=True)
    write_json(task_dir / "candidates.json", candidates)
    write_json(task_dir / "evidence.json", evidence)
    write_json(task_dir / "knowledge-v1.json", {"version": knowledge_version, "items": candidates})
    write_json(release_dir / "manifest.json", manifest)
    prompt = [
        "# Prototype evaluation package",
        "",
        "This package contains approved, source-linked claims only. It is not a production evaluation policy.",
        "",
        *[f"- [{item['candidate_id']}] {item['claim']} (evidence: {', '.join(item['evidence_refs'])})" for item in candidates],
    ]
    (release_dir / "PROMPT.prototype.md").write_text("\n".join(prompt) + "\n", encoding="utf-8")
    task["status"] = "released_prototype"
    task["approved_by"] = reviewer
    task["approved_at"] = approved_at
    write_json(task_dir / "task.json", task)
    print(json.dumps({"task_dir": str(task_dir), "package_id": package_id, "status": task["status"]}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the MVP v1 thin vertical slice.")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_parser = sub.add_parser("ingest", help="Import a TXT and create source/evidence/candidate artifacts.")
    ingest_parser.add_argument("input", type=Path)
    ingest_parser.add_argument("--out", type=Path, default=Path("mvp_runs"))
    ingest_parser.add_argument("--target-speaker")

    approve_parser = sub.add_parser("approve", help="Approve prototype candidates and create a prototype release package.")
    approve_parser.add_argument("task_dir", type=Path)
    approve_parser.add_argument("--reviewer", required=True)

    args = parser.parse_args()
    if args.command == "ingest":
        ingest(args.input, args.out, args.target_speaker)
    else:
        approve(args.task_dir, args.reviewer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
