"""
AI 电话评价 Agent｜LLM 客户端
统一调用大语言模型 API（OpenAI 兼容格式）
"""
import json
import os
import ssl
import subprocess
import time
import urllib.request
import urllib.error


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "llm_config.json")
API_KEY_ENV = "AI_CALL_EVAL_API_KEY"


def load_config():
    """读取 LLM 配置"""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
            config.pop("api_key", None)
            config["api_key"] = os.environ.get(API_KEY_ENV, "")
            return config
    return {
        "base_url": "https://token-hub.in.taou.com",
        "api_key": os.environ.get(API_KEY_ENV, ""),
        "model": "glm-5.2",
        "temperature": 0.7,
        "max_tokens": 65536,
        "timeout": 900,
        "thinking": {"type": "enabled"},
    }


def save_config(config):
    """保存 LLM 配置"""
    config = {key: value for key, value in config.items() if key != "api_key"}
    config["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_safe_config():
    """返回非敏感配置；API Key 只返回是否已外部配置。"""
    config = load_config()
    key = config.get("api_key", "")
    safe = {name: value for name, value in config.items() if name != "api_key"}
    safe["api_key_configured"] = bool(key)
    safe["api_key_source"] = API_KEY_ENV if key else None
    return safe


def set_runtime_api_key(api_key):
    """Set a process-local key without writing it to project files."""
    if api_key:
        os.environ[API_KEY_ENV] = str(api_key)


def _curl_chat(url, api_key, body_json, timeout):
    """Fallback for internal gateways that reject Python/OpenSSL handshakes."""
    quote = lambda value: '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'
    config = "\n".join([
        "url = " + quote(url),
        'request = "POST"',
        "silent",
        "show-error",
        "max-time = " + quote(timeout),
        'header = "Content-Type: application/json"',
        "header = " + quote("Authorization: Bearer " + api_key),
        "data = " + quote(body_json),
        'write-out = "\\n__HTTP_STATUS__:%{http_code}"',
    ])
    try:
        result = subprocess.run(
            ["curl.exe" if os.name == "nt" else "curl", "--config", "-"],
            input=config, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout + 5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": "curl_error", "message": str(exc)}
    output, marker, status_text = result.stdout.rpartition("\n__HTTP_STATUS__:")
    if not marker:
        return {"error": "curl_error", "message": result.stderr.strip() or "missing HTTP status"}
    status = int(status_text or 0)
    if getattr(result, "returncode", 0) or status == 0:
        return {"error": "curl_error", "message": result.stderr.strip() or "request failed without HTTP response"}
    if status >= 400:
        return {"error": "http_error", "status": status, "message": output}
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        return {"error": "invalid_json", "message": str(exc), "raw": output[:500]}
    choice = data.get("choices", [{}])[0]
    content = choice.get("message", {}).get("content", "")
    if not str(content or "").strip():
        return {
            "error": "empty_content",
            "message": "model returned no final content",
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage", {}),
            "raw": data,
        }
    return {"content": content, "model": data.get("model", ""), "usage": data.get("usage", {}), "raw": data}


def chat(messages, **kwargs):
    """
    调用 LLM chat completions API（OpenAI 兼容格式）

    Args:
        messages: [{"role": "user", "content": "..."}]
        **kwargs: temperature, max_tokens, model, timeout

    Returns:
        {"content": "...", "model": "...", "usage": {...}, "raw": {...}}
        或 {"error": "...", "message": "..."}
    """
    config = load_config()

    temperature = kwargs.get("temperature", config.get("temperature", 0.7))
    max_tokens = kwargs.get("max_tokens", config.get("max_tokens", 65536))
    model = kwargs.get("model", config.get("model", "glm-5.2"))
    timeout = kwargs.get("timeout", config.get("timeout", 60))

    base_url = config.get("base_url", "").rstrip("/")
    api_key = config.get("api_key", "")

    if not api_key:
        return {"error": "api_key_not_set", "message": "LLM API Key is not configured"}

    url = base_url + ("/chat/completions" if base_url.endswith("/v1") else "/v1/chat/completions")

    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    thinking = kwargs.get("thinking", config.get("thinking"))
    if thinking is not None:
        body["thinking"] = thinking

    body_text = json.dumps(body, ensure_ascii=False)
    body_json = body_text.encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body_json,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            if not str(content or "").strip():
                return {
                    "error": "empty_content",
                    "message": "model returned no final content",
                    "finish_reason": choice.get("finish_reason"),
                    "usage": data.get("usage", {}),
                    "raw": data,
                }
            return {
                "content": content,
                "model": data.get("model", model),
                "usage": data.get("usage", {}),
                "raw": data,
            }
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass
        return {"error": "http_error", "status": e.code, "message": error_body}
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            return _curl_chat(url, api_key, body_text, timeout)
        return {"error": "url_error", "message": str(e.reason)}
    except Exception as e:
        return {"error": "unknown", "message": str(e)}


def get_config_snapshot():
    """返回编译 manifest 用的 LLM 配置快照（不含 API Key）"""
    config = load_config()
    return {
        "base_url": config.get("base_url", ""),
        "model": config.get("model", ""),
        "temperature": config.get("temperature", 0.7),
        "max_tokens": config.get("max_tokens", 65536),
        "timeout": config.get("timeout", 900),
        "thinking": config.get("thinking", {"type": "enabled"}),
        "max_utterances_per_call": config.get("max_utterances_per_call", 10),
    }
