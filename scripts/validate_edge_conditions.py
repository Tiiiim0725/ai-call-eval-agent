"""Regression checks for executable Graph edge-condition semantics."""
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import app  # noqa: E402


graph = {
    "nodes": [{"id": node_id, "label": node_id} for node_id in ("a", "b", "c", "d", "e", "f")],
    "edges": [
        {"id": "explicit", "source": "a", "target": "b", "label": "候选人说现在方便"},
        {"id": "sequence", "source": "b", "target": "c", "label": ""},
        {"id": "missing-1", "source": "c", "target": "d", "label": ""},
        {"id": "missing-2", "source": "c", "target": "e", "label": ""},
        {"id": "explicit-branch", "source": "f", "target": "e", "label": "候选人明确拒绝"},
    ],
}

classified, issues = app.classify_graph_edge_conditions(graph)
by_id = {edge["id"]: edge for edge in classified["edges"]}

assert by_id["explicit"]["condition_kind"] == "explicit"
assert by_id["explicit"]["condition"] == "候选人说现在方便"
assert by_id["sequence"]["condition_kind"] == "implicit_sequence"
assert "完成上一步后继续" in by_id["sequence"]["condition_display"]
assert by_id["missing-1"]["condition_kind"] == "missing_branch_condition"
assert by_id["missing-2"]["condition_kind"] == "missing_branch_condition"
assert {item["edge_id"] for item in issues} == {"missing-1", "missing-2"}

# Classification is derived: the caller's immutable graph remains untouched.
assert "condition_kind" not in graph["edges"][0]

print("edge condition contract PASS")
