"""可写数据位置的单一来源,与代码位置解耦,且感知打包态(PyInstaller)。

三层位置:
  - code_root(): 只读代码/静态资源(public/)。开发态=仓库根;打包后=PyInstaller 解包目录 _MEIPASS。
  - home(): 所有可写数据(SQLite、连接凭证、导入的策略文件)的根。
      优先级: 环境变量 PAPER_TRADING_HOME > (打包态? 用户数据目录 : 仓库根)。
  - user_data_dir(): 平台标准用户数据目录(打包后默认落这里,保证可写、可持久、各用户隔离)。

为什么这样:打包成桌面 app 后,代码在只读 bundle 里,数据必须落到用户可写目录;
这样更新 app(换 bundle)不动用户数据,每个同事的数据天然各自隔离在本机。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
# 向后兼容:历史代码/测试引用 paths.ROOT(开发态=仓库根)。
ROOT = _SOURCE_ROOT


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def code_root() -> Path:
    if frozen():
        return Path(getattr(sys, "_MEIPASS", _SOURCE_ROOT))
    return _SOURCE_ROOT


def user_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "PaperTrading"


def pointer_file() -> Path:
    """记录用户选择的数据目录的"指针文件",放在固定位置(不依赖数据目录本身)。
    优先级: 环境变量 PAPER_TRADING_POINTER(测试用) > ~/.papertrading/home。"""
    raw = os.environ.get("PAPER_TRADING_POINTER")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".papertrading" / "home"


def _read_pointer() -> Path | None:
    pointer = pointer_file()
    if not pointer.exists():
        return None
    try:
        text = pointer.read_text(encoding="utf-8").strip()
        return Path(text).expanduser() if text else None
    except OSError:
        return None


def home() -> Path:
    # 优先级: 环境变量 > 用户在 app 里选过的位置(指针文件) > (打包?用户目录:仓库根)。
    raw = os.environ.get("PAPER_TRADING_HOME")
    if raw:
        return Path(raw).expanduser()
    pointed = _read_pointer()
    if pointed is not None:
        return pointed
    return user_data_dir() if frozen() else _SOURCE_ROOT


def set_home_pointer(path: str | Path) -> Path:
    """把数据目录指到 path(创建之),写入指针文件。下次启动生效。"""
    target = Path(path).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    pointer = pointer_file()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(target), encoding="utf-8")
    return target


def clear_home_pointer() -> None:
    pointer_file().unlink(missing_ok=True)


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    return _ensure(home() / "data")


def db_path() -> Path:
    return data_dir() / "audit.sqlite3"


def connector_settings_path() -> Path:
    return data_dir() / "connector_settings.json"


def security_names_path() -> Path:
    return data_dir() / "security_names.json"


def security_industries_path() -> Path:
    return data_dir() / "security_industries.json"


def strategies_dir() -> Path:
    return _ensure(home() / "strategies")


def timing_strategies_dir() -> Path:
    return _ensure(home() / "timing_strategies")


def public_dir() -> Path:
    # 前端静态资源跟代码走(打包进 bundle,从 code_root 读)。
    return code_root() / "public"
