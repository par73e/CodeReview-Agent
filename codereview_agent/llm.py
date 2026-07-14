"""Small provider adapters; review logic never depends on provider-specific payloads."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import AppConfig
from .types import Usage


class ModelError(RuntimeError):
    pass


@dataclass
class ModelReply:
    content: str
    usage: Usage


class ModelClient:
    def review(self, system: str, user: str, max_tokens: int) -> ModelReply:
        raise NotImplementedError


class DeepSeekClient(ModelClient):
    def __init__(self, config: AppConfig):
        self.config = config

    def review(self, system: str, user: str, max_tokens: int) -> ModelReply:
        payload = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        data = _post_json(self.config.base_url + "/chat/completions", payload, {"Authorization": "Bearer " + self.config.api_key})
        try:
            usage = data.get("usage", {})
            return ModelReply(
                content=data["choices"][0]["message"]["content"],
                usage=Usage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), int(usage.get("total_tokens", 0))),
            )
        except (KeyError, IndexError, TypeError) as error:
            raise ModelError("DeepSeek 返回内容不完整：{0}".format(error))


class OllamaClient(ModelClient):
    def __init__(self, config: AppConfig):
        self.config = config

    def review(self, system: str, user: str, max_tokens: int) -> ModelReply:
        payload = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": max_tokens},
        }
        data = _post_json(self.config.base_url + "/api/chat", payload)
        message = data.get("message", {})
        content = message.get("content")
        if not content:
            raise ModelError("Ollama 没有返回可解析的审查内容。")
        prompt = int(data.get("prompt_eval_count", 0))
        completion = int(data.get("eval_count", 0))
        return ModelReply(content, Usage(prompt, completion, prompt + completion))


def make_client(config: AppConfig) -> Optional[ModelClient]:
    if config.provider == "deepseek":
        return DeepSeekClient(config)
    if config.provider == "ollama":
        return OllamaClient(config)
    return None


def _post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    request_headers = {"Content-Type": "application/json", "User-Agent": "CodeReview-Agent/0.1"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:500]
        raise ModelError("模型服务返回 HTTP {0}：{1}".format(error.code, body))
    except urllib.error.URLError as error:
        raise ModelError("无法连接模型服务：{0}".format(error.reason))
    except json.JSONDecodeError as error:
        raise ModelError("模型服务返回了非 JSON 内容：{0}".format(error))
