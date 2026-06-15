"""应用级设置(持久化在数据目录,跟随用户数据)。

目前只有"全局默认数据源":各后端接口在未显式指定数据源时,统一回退到这里;
前端"默认数据源"一键改它,所有视图下拉随之预选(各板块仍可单独覆盖)。

代码默认 = tongdaxin(面向真实 A 股使用的用户);开发/测试想用 fixture 时,
设环境变量或在数据源页改即可,不影响线上默认。
"""

from __future__ import annotations

import json
import os
from typing import Any

from backend import paths

CODE_DEFAULT_DATA_SOURCE = "tongdaxin"


def _path():
    return paths.data_dir() / "app_settings.json"


def load() -> dict[str, Any]:
    path = _path()
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def default_data_source() -> str:
    # 环境变量优先(开发/测试可强制 fixture,不污染线上默认)。
    env = os.environ.get("PT_DEFAULT_DATA_SOURCE")
    if env:
        return env.strip().lower()
    value = load().get("default_data_source")
    return str(value).lower() if value else CODE_DEFAULT_DATA_SOURCE


def set_default_data_source(name: str) -> dict[str, Any]:
    clean = str(name or "").strip().lower()
    if not clean:
        raise ValueError("data_source 不能为空")
    data = load()
    data["default_data_source"] = clean
    _save(data)
    return {"default_data_source": clean}
