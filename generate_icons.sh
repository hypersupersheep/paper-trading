#!/usr/bin/env bash
# 单一图标源:public/icon.svg → assets/PaperTrading.icns + .ico。
# 改 public/icon.svg 后跑此脚本,app 图标与 web 左上 logo 即保持一致。
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
PY="$REPO/.venv/bin/python"
SVG="$REPO/public/icon.svg"
TMP="$(mktemp -d)"

# 1) SVG → 1024 PNG(playwright 渲染,透明圆角外)
"$PY" - "$SVG" "$TMP/icon_1024.png" <<'PYEOF'
import sys
from playwright.sync_api import sync_playwright
svg, out = sys.argv[1], sys.argv[2]
markup = open(svg, encoding="utf-8").read()
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 512, "height": 512}, device_scale_factor=2)
    pg.set_content(f'<body style="margin:0;width:512px;height:512px">{markup}</body>')
    pg.wait_for_timeout(250)
    pg.screenshot(path=out, omit_background=True, clip={"x": 0, "y": 0, "width": 512, "height": 512})
    b.close()
PYEOF

# 2) PNG → .icns(iconset + iconutil,macOS)
ICONSET="$TMP/icon.iconset"; mkdir -p "$ICONSET"
for s in 16 32 64 128 256 512 1024; do
  sips -z $s $s "$TMP/icon_1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
done
cp "$ICONSET/icon_32x32.png"     "$ICONSET/icon_16x16@2x.png"
cp "$ICONSET/icon_64x64.png"     "$ICONSET/icon_32x32@2x.png"
cp "$ICONSET/icon_256x256.png"   "$ICONSET/icon_128x128@2x.png"
cp "$ICONSET/icon_512x512.png"   "$ICONSET/icon_256x256@2x.png"
cp "$ICONSET/icon_1024x1024.png" "$ICONSET/icon_512x512@2x.png"
iconutil -c icns "$ICONSET" -o "$REPO/assets/PaperTrading.icns"

# 3) PNG → .ico(Pillow)
"$PY" -c "from PIL import Image; Image.open('$TMP/icon_1024.png').save('$REPO/assets/PaperTrading.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"

rm -rf "$TMP"
echo "✅ 图标已同步: assets/PaperTrading.icns + .ico (源: public/icon.svg)"
