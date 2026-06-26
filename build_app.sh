#!/usr/bin/env bash
# 构建桌面 app 并打成可分享的 zip。需先在 .venv 里装好 pyinstaller。
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
PYI=.venv/bin/pyinstaller

if [ ! -x "$PYI" ]; then
  echo "未找到 pyinstaller,先安装: $PY -m pip install pyinstaller"
  exit 1
fi

echo "==> 清理旧产物"
rm -rf build dist

echo "==> PyInstaller 打包"
"$PYI" --noconfirm --clean paper_trading.spec

VERSION="$("$PY" -c 'from backend.version import __version__; print(__version__)')"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
ZIP="PaperTrading-v${VERSION}-${OS}-${ARCH}.zip"

# macOS 产出 .app(可拖进应用程序);其它平台产出可执行目录。
if [ -d "dist/PaperTrading.app" ]; then
  TARGET="PaperTrading.app"
else
  TARGET="PaperTrading"
fi

echo "==> 打包 zip: dist/${ZIP}"
# .app 必须用 ditto:bundle 里有数百个符号链接,普通 zip 会破坏,解压出来双击"无法打开"。
if [ "$TARGET" = "PaperTrading.app" ]; then
  ( cd dist && rm -f "$ZIP" && ditto -c -k --keepParent "$TARGET" "$ZIP" )
else
  ( cd dist && rm -f "$ZIP" && zip -r -q "$ZIP" "$TARGET" )
fi

echo ""
echo "完成 ✅"
echo "  产物:       dist/${TARGET}"
echo "  分享给同事: dist/${ZIP}"
if [ "$TARGET" = "PaperTrading.app" ]; then
  echo "  安装:       拖进「应用程序」,或 cp -R dist/PaperTrading.app /Applications/"
  echo "  使用:       Launchpad/应用程序里双击,弹出原生窗口"
fi
