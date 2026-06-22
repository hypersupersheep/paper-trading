"""Admin 对接配置(账户级登记的目标 + 本节点身份)。

落在 PAPER_TRADING_HOME/data/admin_link.json,跟随用户数据。**opt-in**:没配 admin_url
就是纯本地模式,登记一律跳过,行为零变化。token 不回前端明文(只回是否已设)。
"""

from __future__ import annotations

import json
import secrets
import socket
import time
import urllib.request
import uuid
from typing import Any

from backend import paths
from backend.version import API_VERSION

_FIELDS = ("admin_url", "admin_token", "node_id", "node_token", "node_name", "base_url")
# 节点反控 token(Admin 远程拉/控本节点时带 X-Admin-Token=node_token);与出站的 admin_token(共享密钥)是两套。

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


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
    data["node_token"] = data.get("node_token") or secrets.token_urlsafe(24)
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


def node_token() -> str:
    """节点反控 token(稳定,自动生成并落盘)。放进登记报文 node.token,Admin 反控时凭它。"""
    data = load()
    if data.get("node_token"):
        return data["node_token"]
    return save({})["node_token"]


def is_loopback(client_ip: Any) -> bool:
    ip = str(client_ip or "")
    return ip in _LOOPBACK or ip.startswith("127.")


def authorize(client_ip: Any, header_token: Any) -> bool:
    """节点入站鉴权:本机(loopback)放行;远程必须带正确 X-Admin-Token = node_token。

    默认 HOST=127.0.0.1 时根本到不了远程分支;只有绑 0.0.0.0/局域网共享时,远程请求才需鉴权
    ——堵住「同网段裸奔」。本地浏览器 UI / agent SDK 走 loopback,不受影响。
    """
    if is_loopback(client_ip):
        return True
    token = node_token()
    return bool(token) and str(header_token or "") == token


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


def node_descriptor(port: int) -> dict[str, Any]:
    """登记报文的 node 段:本节点身份(传输层)。node.token 暂空(节点鉴权属 node_patch)。"""
    cfg = load()
    base = cfg.get("base_url") or f"http://{lan_ip()}:{port}"
    name = cfg.get("node_name") or socket.gethostname().split(".")[0] or "node"
    # node.token = 节点反控 token,Admin 反控时带 X-Admin-Token=它。
    return {"id": node_id(), "name": name, "base_url": base, "token": node_token(), "api_version": API_VERSION}


def account_segment(account: dict[str, Any]) -> dict[str, Any]:
    """登记报文的 account 段(account 身份)。owner 缺省回退账户名。"""
    return {
        "id": account.get("id"),
        "owner": account.get("owner") or account.get("name"),
        "name": account.get("name"),
        "currency": account.get("currency", "CNY"),
        "market": account.get("market", "CN_A"),
        "initial_cash": account.get("initial_cash"),
    }


def post(path: str, payload: dict[str, Any], timeout: float = 5.0) -> bool:
    """向 Admin 发一次 POST(best-effort)。X-Admin-Token = Admin 共享密钥(配了才带)。"""
    cfg = load()
    if not cfg.get("admin_url"):
        return False
    try:
        url = str(cfg["admin_url"]).rstrip("/") + path
        headers = {"Content-Type": "application/json"}
        if cfg.get("admin_token"):
            headers["X-Admin-Token"] = cfg["admin_token"]  # Admin 共享密钥,非 node.token
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception:  # noqa: BLE001 - Admin 不可达不影响本地
        return False


def register_node_accounts(port: int, accounts: list[dict[str, Any]], retries: int = 1, delay: float = 0.0) -> bool:
    """批量登记:一次 POST {node, accounts:[...]} 到同一 register 端点(Admin 倾向口径),可重试。"""
    if not is_enabled() or not accounts:
        return False
    payload = {"node": node_descriptor(port), "accounts": [account_segment(a) for a in accounts]}
    for attempt in range(max(1, retries)):
        if post("/api/admin/accounts/register", payload):
            return True
        if delay and attempt < retries - 1:
            time.sleep(delay)
    return False


def deregister_path(account_id: str) -> str:
    return f"/api/admin/accounts/{node_id()}/{account_id}/delete"


def public_view() -> dict[str, Any]:
    """给前端的视图:不回 token 明文,只回是否已设。顺便固化稳定 node_id。"""
    data = load()
    return {
        "admin_url": data.get("admin_url", ""),
        "node_id": node_id(),
        "node_token": node_token(),  # 节点反控 token,展示给本机 owner(可对 Admin 核对)
        "node_name": data.get("node_name", ""),
        "base_url": data.get("base_url", ""),
        "has_token": bool(data.get("admin_token")),
        "enabled": bool(data.get("admin_url")),
    }
