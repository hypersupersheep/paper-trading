#!/usr/bin/env python3
"""桌面 app 启动器 / PyInstaller 入口。

精致版:用原生窗口(pywebview / WKWebView)裹住前端,像真正的桌面应用,而不是跳系统浏览器。

职责:
  1. worker 派发:策略/择时 worker 原本是 `python -m backend.xxx_worker` 子进程;打包后
     sys.executable 是 app 本身,所以被以 `app __worker__ <kind> <payload>` 调用时直接当 worker 跑。
  2. 正常启动:数据目录指向用户可写目录 → 后台起服务 → 弹出原生窗口(先显示启动 splash,
     服务就绪后载入界面)。关窗即退,Cmd-Q 直接退。
  3. 降级:无 pywebview(或设了 PT_NO_WINDOW)时回退到系统浏览器/前台服务,便于开发与自动化测试。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser


SPLASH_HTML = """
<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:#0e1117;color:#e6edf3;
    font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;overflow:hidden}
  .wrap{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:22px}
  .mark{width:72px;height:72px;border-radius:16px;display:grid;place-items:center;font-weight:800;
    font-size:24px;color:#fff;background:linear-gradient(135deg,#2f81f7,#1f6fe0);letter-spacing:1px}
  .title{font-size:18px;font-weight:600;color:#f4f8fd}
  .sub{font-size:13px;color:#7d8590}
  .bar{width:180px;height:3px;border-radius:3px;background:#1c2230;overflow:hidden}
  .bar i{display:block;height:100%;width:40%;border-radius:3px;background:#2f81f7;
    animation:slide 1.1s ease-in-out infinite}
  @keyframes slide{0%{margin-left:-40%}100%{margin-left:100%}}
</style></head><body><div class="wrap">
  <div class="mark">QR</div>
  <div class="title">量化模拟盘</div>
  <div class="sub">正在启动本地引擎…</div>
  <div class="bar"><i></i></div>
</div></body></html>
"""


def _maybe_run_worker() -> None:
    """打包态下,被当作策略/择时 worker 调用时,派发后退出。"""
    if len(sys.argv) >= 4 and sys.argv[1] == "__worker__":
        kind, payload = sys.argv[2], sys.argv[3]
        sys.argv = [sys.argv[0], payload]
        if kind == "timing":
            from backend.timing_worker import main as worker_main
        else:
            from backend.strategy_worker import main as worker_main
        raise SystemExit(worker_main())


def _free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _run_server() -> None:
    # 服务跑在 daemon 线程里;之前这里没有兜底——run() 一抛异常线程就静默死掉,
    # 打包 app 没终端,表现为永远卡在"正在启动本地引擎"。这里把致命异常落盘到数据目录的
    # startup_error.log(同时打到 stderr,PT_NO_WINDOW 模式可见),让启动失败可诊断、可上报。
    try:
        from backend.server import run

        run()
    except BaseException:  # noqa: BLE001 - 启动期任何异常都要留痕,否则用户只看到无限 splash
        import datetime
        import traceback

        tb = traceback.format_exc()
        sys.stderr.write(tb)
        try:
            from backend import paths

            log_path = paths.home() / "startup_error.log"
            log_path.write_text(
                f"[{datetime.datetime.now().isoformat()}] paper-trading 引擎启动失败:\n{tb}\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - 连日志都写不了就算了,至少 stderr 有
            pass
        raise


def main() -> int:
    _maybe_run_worker()

    from backend import paths

    os.environ.setdefault("PAPER_TRADING_HOME", str(paths.home()))
    port = int(os.environ.get("PORT") or _free_port(8000))
    os.environ["PORT"] = str(port)
    url = f"http://127.0.0.1:{port}"

    # 无窗口模式(开发/自动化测试/纯服务):前台跑服务,便于 curl。
    if os.environ.get("PT_NO_WINDOW"):
        print(f"数据目录: {paths.home()}", flush=True)
        print(f"服务: {url}", flush=True)
        _run_server()
        return 0

    # 后台起服务(原生窗口与浏览器都连它)。
    threading.Thread(target=_run_server, daemon=True).start()
    ready = _wait_until_ready(port)

    def _browser_fallback(reason: str = "") -> int:
        """原生窗口不可用时回退系统浏览器,保证 app 一定能用(本身就是网页 UI)。"""
        if reason:
            print(f"原生窗口不可用,回退浏览器: {reason}", flush=True)
        if ready or _wait_until_ready(port):
            webbrowser.open(url)
        threading.Event().wait()  # 保持进程存活(服务在守护线程里)
        return 0

    # 优先原生窗口;任何失败都回退浏览器:
    #   - 没装 pywebview(ImportError);
    #   - Windows 上 pywebview 的 .NET(pythonnet/clr)后端起不来 —— 这类异常发生在 webview.start()
    #     里(import winforms→clr),只 catch ImportError 兜不住,必须 catch 全部异常。
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        return _browser_fallback(f"pywebview 不可用({exc})")

    try:
        class _DesktopApi:
            """注入到前端 window.pywebview.api,提供浏览器做不到的原生能力。"""

            def pick_folder(self):
                result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
                if not result:
                    return None
                return result[0] if isinstance(result, (list, tuple)) else str(result)

            def restart(self):
                import subprocess

                subprocess.Popen([sys.executable])  # 新实例会读新数据目录指针
                for win in list(webview.windows):
                    win.destroy()
                return True

        window = webview.create_window(
            "量化模拟盘 Paper Trading",
            url=(url if ready else None),
            html=(None if ready else SPLASH_HTML),
            width=1480,
            height=940,
            min_size=(1120, 720),
            background_color="#0e1117",
            js_api=_DesktopApi(),
        )

        def _boot() -> None:
            if not ready and _wait_until_ready(port):
                window.load_url(url)  # 服务慢时:从 splash 切到真界面

        webview.start(_boot)  # 主线程跑 GUI;窗口关闭后返回 → 进程退出。后端起不来会在这里抛
        return 0
    except Exception as exc:  # noqa: BLE001 - 原生窗口后端起不来(如 Windows .NET)→ 回退浏览器
        return _browser_fallback(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
