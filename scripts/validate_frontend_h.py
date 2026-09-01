"""Frontend/backend contract regression for the H-aligned MVP."""
import json
import pathlib
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, dict(response.headers), response.read().decode("utf-8")


def main():
    app_js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    for marker in ("approve-g1", "approve-graph", "reject-graph", "/graph-review",
                   "graph-canvas", "compile-prompts", "approve-g5", "create-release",
                   "import-content", "choose-txt", "include_all: true", "graph-mode",
                   "/edge-condition", "save-edge-condition", "reset-edge-condition",
                   "/generate-execution-prompt", "execution_prompt_sha256",
                   "route_table_sha256", "/node-content", "save-node-content",
                   "/graph-export", "导出当前 Graph JSON", "/graph-layout",
                   "电话流程（七阶段）", "save-layout-annotation", "恢复自动判断",
                   "context-muted", "control-point-distances", "相对来源节点",
                   "dragfree", "position_updates", "graph-layout-reset", "重新初始化布局",
                   "LLM 生成电话执行 Prompt", "LLM 负责生成执行说明层"):
        assert marker in app_js, marker
    assert 'data-view="evidence"' not in index
    assert 'src="app.js?v=0.53.0"' in index, "frontend bundle must be cache-busted for v0.53 issue locators"
    assert 'href="styles.css?v=0.53.0"' in index, "frontend styles must be cache-busted for v0.53 issue locators"
    assert "delete-task" in app_js and "/tasks/delete" in app_js
    for mojibake in ("锛", "鈥", "宸ヤ綔", "鐢佃瘽"):
        assert mojibake not in app_js + index, mojibake

    front_status, _, front_body = get("http://127.0.0.1:8897/")
    health_status, health_headers, health_body = get("http://127.0.0.1:8898/api/health")
    source_status, _, source_body = get(
        "http://127.0.0.1:8898/api/sources?task_id=task_480709a4c7f6")
    config_status, _, config_body = get("http://127.0.0.1:8898/api/llm-config")
    sources = json.loads(source_body).get("sources", [])
    assert front_status == health_status == source_status == config_status == 200
    assert "评价策略台" in front_body
    assert json.loads(health_body)["status"] == "ok"
    assert health_headers.get("Access-Control-Allow-Origin") != "*"
    assert all("snapshot" not in source for source in sources)
    assert all(source.get("snapshot_redacted") is True for source in sources)
    config = json.loads(config_body)
    assert config.get("model")
    assert "api_key" not in config
    print(json.dumps({
        "status": "PASS", "frontend": front_status, "backend": health_status,
        "model": config.get("model"), "sources_redacted": len(sources),
        "controls": 26,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
