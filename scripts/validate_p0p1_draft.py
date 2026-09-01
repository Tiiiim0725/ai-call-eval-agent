"""Small consistency check for the P0/P1 decision draft.

This intentionally validates only the review artifact, not the product itself.
"""

from pathlib import Path


DOC = Path(__file__).resolve().parents[1] / "docs" / "P0-P1决策草案_v0.21.md"


def main() -> int:
    text = DOC.read_text(encoding="utf-8")
    p0 = ["02", "03", "04", "05", "07", "08", "09", "10", "11"]
    p1 = [f"{i:02d}" for i in range(1, 9)]
    required = [*(f"OPEN-P0-{i}" for i in p0), *(f"OPEN-P1-{i}" for i in p1)]
    missing = [item for item in required if item not in text]

    required_terms = [
        "source_id",
        "sha256",
        "evidence_refs",
        "manifest.json",
        "prompt_sha256",
        "created -> imported -> parsed",
        "golden_case_set",
    ]
    missing_terms = [item for item in required_terms if item not in text]

    if missing or missing_terms:
        if missing:
            print("missing IDs:", ", ".join(missing))
        if missing_terms:
            print("missing terms:", ", ".join(missing_terms))
        return 1

    print(f"OK: {len(required)} open-item IDs and {len(required_terms)} contract terms found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
