"""进程内事件总线(给 SSE 用):成交等事件发生即 fan-out 给所有订阅者。

纯标准库,无网络认知 —— trading_store 直接 publish,SSE 端点 subscribe。
慢消费者(队列满)丢弃事件而非阻塞,绝不拖累交易主流程。
"""

from __future__ import annotations

import queue
import threading
from typing import Any

_subscribers: list["queue.Queue[dict[str, Any]]"] = []
_lock = threading.Lock()


def subscribe(maxsize: int = 200) -> "queue.Queue[dict[str, Any]]":
    q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=maxsize)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: "queue.Queue[dict[str, Any]]") -> None:
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def publish(event: dict[str, Any]) -> None:
    """把事件分发给所有订阅者。队列满(慢消费者)就丢弃该条,不阻塞 publisher。"""
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)
