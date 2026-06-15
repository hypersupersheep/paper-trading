"""策略/择时 worker 子进程的命令构造(中立模块,避免 store 之间循环 import)。"""

from __future__ import annotations

import sys


def worker_command(kind: str, payload_path: str) -> list[str]:
    """开发态: `python -m backend.{kind}_worker payload`。
    打包(PyInstaller frozen)态: sys.executable 是 app 本身,改用 app 的 __worker__ 派发
    (见 launcher.py 的 _maybe_run_worker)。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "__worker__", kind, payload_path]
    return [sys.executable, "-m", f"backend.{kind}_worker", payload_path]
