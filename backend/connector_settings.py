from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import paths


# 数据源凭证存本地 JSON 文件而不是 SQLite：
# 多个 DataConnectorRegistry 实例(strategy/timing 各持一个) 都要读到同一份配置，
# 且密钥绝不能进审计流水，文件层隔离最简单。位置随 paths(可由 PAPER_TRADING_HOME 覆盖)。


def _default_path() -> Path:
    return paths.connector_settings_path()


def load_settings(path: Path | None = None) -> dict[str, Any]:
    target = path or _default_path()
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_connector_settings(name: str, values: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    target = path or _default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    settings = load_settings(target)
    settings[name] = {**values, "updated_at": datetime.now(timezone.utc).isoformat()}
    target.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings[name]


def get_connector_settings(name: str, path: Path | None = None) -> dict[str, Any]:
    return load_settings(path).get(name, {})


def mask_secret(value: str | None) -> str | None:
    """密钥只在 UI 上展示首尾片段，完整值永不离开配置文件。"""
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"
