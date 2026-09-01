"""Static guardrails for the Graph-centred review workflow."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
index_html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

assert 'data-view="evidence"' not in index_html, "03 证据审核仍暴露在主导航"
assert 'data-go="evidence"' not in app_js, "工作台仍把人工流程导向证据页"
assert "graphCandidateId" in app_js, "无法选择候选 Graph 版本"
assert "materializedCandidate" in app_js, "候选模式没有物化为基线加增量变更"
assert 'value="diff"' in app_js, "统一画布缺少差异模式"
assert "script_evidence_refs" in app_js and "evidence-card" in app_js, "节点详情没有原话证据与回听定位"
assert "/graph-review" in app_js, "Graph 页面没有整图审核入口"
assert "include_all: true" in app_js, "正式 Graph 学习仍可能走截断入口"
assert "classifyDisplayedEdgeConditions" in app_js, "Graph 画布没有区分显式、顺序与缺失条件"
assert "missing_branch_condition" in app_js, "缺失分支条件没有显式告警/阻断"
assert "edge.label || edge.condition || edge.id" not in app_js, "Graph 仍用边 ID 冒充条件"
assert "/script-variants" in app_js and "/script-selections" in app_js, "Graph 右栏没有接入原话版本与选择接口"
assert "保存为新版本" in app_js and "重置为访谈初始版" in app_js, "原话版本操作不完整"
assert "永久删除此版本" in app_js and "save-script-selections" in app_js, "原话删除或多选保存入口缺失"
assert "/edge-condition" in app_js and "保存并确认条件" in app_js and "重置为原始条件" in app_js, "边条件校准入口不完整"
assert "review_required_condition" in app_js, "明确不确定的问题边没有可视化状态"
assert "请先保存或重置右栏未保存修改" in app_js, "未保存右栏修改未阻断整图批准"
assert "/node-content" in app_js and "保存节点修改" in app_js and "重置为原始内容" in app_js, "导入/候选节点编辑入口不完整"
assert "/graph-export" in app_js and "导出当前 Graph JSON" in app_js, "规范 Graph JSON 导出入口缺失"
assert "/graph-layout" in app_js and "电话流程（七阶段）" in app_js, "电话流程布局入口缺失"
assert "callFlowLayout" in app_js and "layout-phase" in app_js and "layout-tendency" in app_js, "布局分类或校准控件缺失"
assert ".drawio,.drawio.xml,.xml,.json" in app_js, "规范 Graph JSON 不能重新导入"

print("graph-centred workflow PASS")
