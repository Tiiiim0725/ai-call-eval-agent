"""Minimal regression check for the TLS curl fallback."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
import llm_client  # noqa: E402


captured = {}
original_run = llm_client.subprocess.run


def fake_run(args, **kwargs):
    captured.update(args=args, config=kwargs["input"])
    return type("Result", (), {
        "stdout": '{"model":"glm-5.2","choices":[{"message":{"content":"ok"}}],"usage":{}}\n__HTTP_STATUS__:200',
        "stderr": "",
    })()


def timeout_run(args, **kwargs):
    return type("Result", (), {
        "stdout": "\n__HTTP_STATUS__:000",
        "stderr": "curl: (28) Operation timed out",
        "returncode": 28,
    })()


def empty_run(args, **kwargs):
    return type("Result", (), {
        "stdout": '{"model":"glm-5.2","choices":[{"finish_reason":"length","message":{"content":"","reasoning_content":"thinking"}}],"usage":{"completion_tokens":128}}\n__HTTP_STATUS__:200',
        "stderr": "",
        "returncode": 0,
    })()


try:
    llm_client.subprocess.run = fake_run
    result = llm_client._curl_chat("https://example.test/v1/chat/completions", "secret", "{}", 5)
finally:
    llm_client.subprocess.run = original_run

assert result["content"] == "ok"
assert "secret" not in " ".join(captured["args"]), "Key leaked into process arguments"
assert "Authorization: Bearer secret" in captured["config"]
llm_client.subprocess.run = timeout_run
try:
    timeout_result = llm_client._curl_chat("https://example.test/v1/chat/completions", "secret", "{}", 5)
finally:
    llm_client.subprocess.run = original_run
assert timeout_result["error"] == "curl_error" and "timed out" in timeout_result["message"]
llm_client.subprocess.run = empty_run
try:
    empty_result = llm_client._curl_chat("https://example.test/v1/chat/completions", "secret", "{}", 5)
finally:
    llm_client.subprocess.run = original_run
assert empty_result["error"] == "empty_content" and empty_result["finish_reason"] == "length"
print("llm curl fallback PASS")
