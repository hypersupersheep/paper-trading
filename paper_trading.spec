# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置:把模拟盘打成自包含的桌面 app。

入口 launcher.py(它会起服务、开浏览器,并在被当 worker 调用时派发)。
public/(含 lightweight-charts vendor)和 agent/(SDK/CLI/SKILL) 作为数据一起打进 bundle。
重依赖(pandas/numpy/mootdx/rqdatac/pymysql)默认排除以保持精简——核心(fixture 行情 +
模拟/回测/绩效/agent)全是标准库,照常工作;真实数据源(米筐/通达信/wind)需要时另行安装这些依赖
(见 BUILD.md 的"完整版"说明)。
"""

import sys

block_cipher = None
# 按平台选图标:Windows 用 .ico,macOS 用 .icns。
_ICON = "assets/PaperTrading.ico" if sys.platform.startswith("win") else "assets/PaperTrading.icns"

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("public", "public"),
        ("agent", "agent"),
    ],
    # 这两个 worker 是运行时动态 import 的(launcher 的 __worker__ 派发),显式声明免得被漏掉。
    hiddenimports=["backend.strategy_worker", "backend.timing_worker"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pandas", "numpy", "mootdx", "rqdatac", "pymysql", "tushare",
        "matplotlib", "scipy", "tkinter", "PIL", "pytest", "playwright",
    ],
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
            "CFBundleShortVersionString": "1.1.0",
            "CFBundleVersion": "1.1.0",
            "NSHighResolutionCapable": True,
        },
    )

