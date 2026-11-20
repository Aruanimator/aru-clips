#!/bin/sh
# Builds dist/AruClips.exe (run from Linux or macOS with Go installed)
set -e
cd "$(dirname "$0")"
ROOT=..
rm -f payload.zip
( cd "$ROOT" && find app -name __pycache__ -exec rm -rf {} + ; rm -f launcher/payload.zip && zip -q -X -r launcher/payload.zip app requirements.txt )
${RSRC:-rsrc} -manifest app.manifest -ico icon.ico -arch amd64 -o rsrc_windows_amd64.syso
mkdir -p ../dist
[ -f go.work ] || [ -f go.sum ] || go mod tidy  # first build: fetch the Go modules
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-H windowsgui -s -w" -o ../dist/AruClips.exe .
ls -la ../dist/AruClips.exe
