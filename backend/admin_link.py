"""Admin 对接配置(账户级登记的目标 + 本节点身份)。

落在 PAPER_TRADING_HOME/data/admin_link.json,跟随用户数据。**opt-in**:没配 admin_url
就是纯本地模式,登记一律跳过,行为零变化。token 不回前端明文(只回是否已设)。
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any

from backend import paths

_FIELDS = ("admin_url", "admin_token", "node_id", "node_name", "base_url")


def _path():
    return paths.data_dir() / "admin_link.json"


def load() -> dict[str, Any]:
    try:
        path = _path()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    return {k: data.get(k, "") for k in _FIELDS}


def save(updates: dict[str, Any]) -> dict[str, Any]:
    data = load()
    for key in _FIELDS:
        if key in updates and updates[key] is not None:
            value = str(updates[key]).strip()
            # 空串视为"不改"(token 尤其:前端不回明文,留空即保留原值)。
            if value or key in {"admin_url"}:
                data[key] = value
    data["node_id"] = data.get("node_id") or _new_node_id()
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return data


def node_id() -> str:
    """稳定节点 id:已有则用,没有则生成并落盘。"""
    data = load()
    if data.get("node_id"):
        return data["node_id"]
    return save({})["node_id"]


def _new_node_id() -> str:
    host = "".join(c for c in socket.gethostname().split(".")[0] if c.isalnum() or c in "-_")[:24] or "node"
    return f"{host}-{uuid.uuid4().hex[:6]}"


def is_enabled() -> bool:
    return bool(load().get("admin_url"))


def lan_ip() -> str:
    """本机在局域网里的 IP(不真正发包,只问内核选哪个出口)。失败回环兜底。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        sock.close()


def public_view() -> dict[str, Any]:
    """给前端的视图:不回 token 明文,只回是否已设。顺便固化稳定 node_id。"""
    data = load()
    return {
        "admin_url": data.get("admin_url", ""),
        "node_id": node_id(),
        "node_name": data.get("node_name", ""),
        "base_url": data.get("base_url", ""),
        "has_token": bool(data.get("admin_token")),
        "enabled": bool(data.get("admin_url")),
    }
