"""Persistent per-user model configuration."""

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


CONFIG_DIR = Path.home() / ".codereview"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class AppConfig:
    provider: str = "local"
    model: str = ""
    api_key: str = ""
    base_url: str = ""

    @property
    def configured(self) -> bool:
        if self.provider == "deepseek":
            return bool(self.api_key and self.model and self.base_url)
        if self.provider == "ollama":
            return bool(self.model and self.base_url)
        return self.provider == "local"


def load_config() -> Optional[AppConfig]:
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return AppConfig(**data)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def save_config(config: AppConfig) -> None:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)


def redacted_summary(config: AppConfig) -> str:
    if config.provider == "deepseek":
        return "DeepSeek / {0}".format(config.model)
    if config.provider == "ollama":
        return "Ollama / {0}".format(config.model)
    return "辅助本地检查（无模型）"


def prompt_configuration(existing: Optional[AppConfig] = None) -> AppConfig:
    """Run a deliberately small, guided configuration wizard."""
    print("\n模型配置")
    print("1. DeepSeek API（代码上下文会发送至 DeepSeek）")
    print("2. 本地 Ollama（代码只发送至本机 Ollama 服务）")
    print("3. 辅助本地检查（不使用大模型）")
    choice = input("请选择 [1-3]：").strip()

    if choice == "1":
        key = input("请输入 DeepSeek API Key：").strip()
        model = input("模型名 [deepseek-v4-flash]：").strip() or "deepseek-v4-flash"
        base_url = input("API 地址 [https://api.deepseek.com]：").strip() or "https://api.deepseek.com"
        config = AppConfig("deepseek", model, key, base_url.rstrip("/"))
    elif choice == "2":
        model = input("本地模型名（例如 qwen2.5-coder:7b）：").strip()
        base_url = input("Ollama 地址 [http://localhost:11434]：").strip() or "http://localhost:11434"
        config = AppConfig("ollama", model, "", base_url.rstrip("/"))
    elif choice == "3":
        config = AppConfig()
    else:
        print("输入无效，已选择辅助本地检查。")
        config = AppConfig()

    save_config(config)
    print("配置已保存到 {0}".format(CONFIG_PATH))
    return config
