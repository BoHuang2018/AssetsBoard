#!/bin/bash
# Build ~/Applications/AssetsBoard.app (native window via pywebview) for this checkout.
# - Creates/updates a private venv at <project>/.venv (system Python untouched) and installs pywebview.
# - The app bundle only contains Info.plist, a launcher script and an icon: no secrets, no code copy.
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-/usr/local/bin/python3}"
APP="$HOME/Applications/AssetsBoard.app"

[ -x "$PROJECT/.venv/bin/python" ] || "$PY" -m venv "$PROJECT/.venv"
"$PROJECT/.venv/bin/python" -m pip install --quiet --upgrade pip
"$PROJECT/.venv/bin/python" -m pip install --quiet "pywebview>=5"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$HOME/Library/Logs"
cat > "$APP/Contents/MacOS/AssetsBoard" <<SH
#!/bin/bash
# Launcher: run the dashboard window from the project checkout. No credentials are stored here.
cd "$PROJECT" || exit 1
exec "$PROJECT/.venv/bin/python" -m assetsboard app >>"\$HOME/Library/Logs/AssetsBoard.log" 2>&1
SH
chmod +x "$APP/Contents/MacOS/AssetsBoard"

cat > "$APP/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>AssetsBoard</string>
  <key>CFBundleDisplayName</key><string>AssetsBoard</string>
  <key>CFBundleIdentifier</key><string>local.assetsboard.dashboard</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>AssetsBoard</string>
  <key>CFBundleIconFile</key><string>AssetsBoard</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PL

# icon (generated, stdlib only) -> .icns
TMP="$(mktemp -d)"
"$PROJECT/.venv/bin/python" "$PROJECT/tools/make_icon.py" "$TMP/icon_1024.png"
mkdir -p "$TMP/AssetsBoard.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/icon_1024.png" --out "$TMP/AssetsBoard.iconset/icon_${s}x${s}.png" >/dev/null
  d=$((s*2)); sips -z $d $d "$TMP/icon_1024.png" --out "$TMP/AssetsBoard.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/AssetsBoard.iconset" -o "$APP/Contents/Resources/AssetsBoard.icns"
rm -rf "$TMP"
touch "$APP"
echo "已建立：$APP"
echo "開啟：open \"$APP\"（或在 Finder → 應用程式（個人）→ AssetsBoard）"
