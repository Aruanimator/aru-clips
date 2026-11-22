#!/bin/sh
# Builds dist/Aru-Clips-mac.zip: "Aru Clips.app" for Apple Silicon and Intel Macs (run after build.sh made payload.zip).
# Needs makefat (github.com/randall77/makefat) to join the two builds into one app.
set -e
cd "$(dirname "$0")"
[ -f payload.zip ] || { echo "run build.sh first"; exit 1; }
OUT=$(cd .. && pwd)/dist
WORK=${TMPDIR:-/tmp}/aruc_mac
mkdir -p "$OUT" "$WORK"
GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o "$WORK/arm64" .
GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o "$WORK/amd64" .
APP="$WORK/Aru Clips.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
${MAKEFAT:-makefat} "$APP/Contents/MacOS/AruClips" "$WORK/arm64" "$WORK/amd64"
chmod 755 "$APP/Contents/MacOS/AruClips"
python3 -c "from PIL import Image; Image.open('icon.ico').convert('RGBA').resize((1024,1024)).save('$WORK/AppIcon.icns')"
cp "$WORK/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
VER=$(cat ../app/VERSION)
sed "s/@VER@/$VER/" Info.plist.in > "$APP/Contents/Info.plist"
rm -f "$OUT/Aru-Clips-mac.zip"
( cd "$WORK" && zip -q -r -y "$OUT/Aru-Clips-mac.zip" "Aru Clips.app" )
ls -la "$OUT/Aru-Clips-mac.zip"
