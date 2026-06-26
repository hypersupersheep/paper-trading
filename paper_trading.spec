# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置:把模拟盘打成自包含的桌面 app。

入口 launcher.py(它会起服务、开浏览器,并在被当 worker 调用时派发)。
public/(含 lightweight-charts vendor)和 agent/(SDK/CLI/SKILL) 作为数据一起打进 bundle。

两种构建(默认=完整版):
  - 完整版(默认):把真实数据源依赖(mootdx/pandas/numpy/rqdatac/pymysql)一起打进去,
    通达信/米筐/wind 在桌面 app 里直接可用。包较大。
  - 精简版(设环境变量 PT_LEAN=1):排除上述重依赖,只保留 fixture 合成行情 + 模拟/回测/
    绩效/agent(全标准库),包很小。给只需演示的同事用。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all

block_cipher = None
# 按平台选图标:Windows 用 .ico,macOS 用 .icns。
_ICON = "assets/PaperTrading.ico" if sys.platform.startswith("win") else "assets/PaperTrading.icns"

_LEAN = os.environ.get("PT_LEAN") == "1"
# 真实数据源依赖:完整版打进去,精简版排除。
_HEAVY = ["pandas", "numpy", "mootdx", "rqdatac", "pymysql"]
# 这些无论哪种构建都用不到,始终排除以减肥。
_excludes = ["tushare", "matplotlib", "scipy", "tkinter", "PIL", "pytest", "playwright"]

_datas = [("public", "public"), ("agent", "agent")]
_binaries = []
# 这两个 worker 是运行时动态 import 的(launcher 的 __worker__ 派发),显式声明免得被漏掉。
_hiddenimports = ["backend.strategy_worker", "backend.timing_worker"]

if _LEAN:
    _excludes = _HEAVY + _excludes
else:
    # 完整版:用 collect_all 把数据源包的子模块 + 数据文件 + 动态库全收进来,
    # 避免"打进去了但运行时缺文件"(mootdx 的服务器配置、rqdatac 的 protobuf 等)。
    # 只收集当前环境装了的包;没装的就排除(让该构建环境优雅降级,不至于整体失败)。
    import importlib.util

    for _pkg in _HEAVY:
        if importlib.util.find_spec(_pkg) is None:
            _excludes.append(_pkg)
            continue
        _d, _b, _h = collect_all(_pkg)
        _datas += _d
        _binaries += _b
        _hiddenimports += _h

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PaperTrading",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 原生窗口(pywebview),无终端;关窗或 Cmd-Q 退出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PaperTrading",
)

# macOS .app 包(仅 macOS 生成):可拖进「应用程序」、Launchpad/Finder 双击,Dock 右键「退出」。
# Windows 上 BUNDLE 无意义,产物就是 dist/PaperTrading/PaperTrading.exe(单文件夹,可压缩分发)。
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="PaperTrading.app",
        icon="assets/PaperTrading.icns",
        bundle_identifier="com.quantresearch.papertrading",
        info_plist={
            "CFBundleName": "PaperTrading",
            "CFBundleDisplayName": "量化模拟盘",
            "CFBundleShortVersionString": "1.14.2",
            "CFBundleVersion": "1.14.2",
            "NSHighResolutionCapable": True,
        },
    )

