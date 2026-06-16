#!/usr/bin/env bash
# Build a Chrome Web Store-ready zip of the extension at its current version.
#
# Output: Desktop/Energy Agent/Archives - Extension Builds/energyagent-extension-vX.Y.Z.zip
# and ALSO copied to BOTH Desktop ROOTS (C: and OneDrive) so Ford can grab the
# current build directly without digging into the Archives subfolder.
#
# Reads the version from extension/manifest.json so bumping the manifest is
# the only thing you need to do — never hand-rename zips again.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f extension/manifest.json ]]; then
  echo "ERROR: extension/manifest.json not found" >&2
  exit 1
fi

VERSION="$(python3 -c "import json; print(json.load(open('extension/manifest.json'))['version'])")"
NAME="energyagent-extension-v${VERSION}"

# Prefer the Windows Desktop archive dir (WSL); fall back to a local builds/
ARCHIVE_DIR_WIN="/mnt/c/Users/fordg/Desktop/Energy Agent/Archives - Extension Builds"
ARCHIVE_DIR_LOCAL="$PWD/builds"
if [[ -d "$ARCHIVE_DIR_WIN" ]]; then
  ARCHIVE_DIR="$ARCHIVE_DIR_WIN"
else
  mkdir -p "$ARCHIVE_DIR_LOCAL"
  ARCHIVE_DIR="$ARCHIVE_DIR_LOCAL"
fi

ZIP_PATH="$ARCHIVE_DIR/$NAME.zip"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

# Stage a clean copy — no .DS_Store, no .git*, no Mac/WSL crud.
cp -R extension "$STAGE_DIR/extension"
find "$STAGE_DIR/extension" \
  \( -name ".DS_Store" -o -name "Thumbs.db" -o -name "*.swp" \
     -o -name ".gitkeep" -o -name ".gitignore" \) \
  -delete 2>/dev/null || true

# Sanity check: required files present
for f in manifest.json background.js content.js vec_content.js so_bridge.js \
         popup/popup.html popup/popup.js popup/popup.css \
         icons/icon16.png icons/icon48.png icons/icon128.png; do
  if [[ ! -e "$STAGE_DIR/extension/$f" ]]; then
    echo "ERROR: missing required file extension/$f" >&2
    exit 1
  fi
done

# Build zip — Chrome Web Store requires the manifest at the zip ROOT, not
# nested inside a subfolder. We zip the *contents* of extension/, not the
# folder itself.
rm -f "$ZIP_PATH"
(cd "$STAGE_DIR/extension" && zip -qr "$ZIP_PATH" .)

# Also drop an unzipped copy alongside so "Load unpacked" works without
# an extra extract step on Chrome dev machines.
UNZIPPED_DIR="$ARCHIVE_DIR/$NAME"
rm -rf "$UNZIPPED_DIR"
cp -R "$STAGE_DIR/extension" "$UNZIPPED_DIR"

SIZE_KB=$(($(stat -c%s "$ZIP_PATH") / 1024))
echo "✓ Built: $ZIP_PATH (${SIZE_KB}KB)"
echo "✓ Unpacked copy: $UNZIPPED_DIR"

# Copy the current build to BOTH Desktop ROOTS so it's grabbable without digging
# into the Archives subfolder (Ford repeatedly couldn't find zips left only in
# Archives). Non-fatal if a root doesn't exist (e.g. no OneDrive on this machine).
for ROOT in "/mnt/c/Users/fordg/Desktop" "/mnt/c/Users/fordg/OneDrive/Desktop"; do
  if [[ -d "$ROOT" ]]; then
    cp -f "$ZIP_PATH" "$ROOT/" 2>/dev/null \
      && echo "✓ Copied to Desktop root: $ROOT/$NAME.zip" \
      || echo "⚠ Could not copy to $ROOT (skipped)"
  fi
done
echo ""
echo "Next steps:"
echo "  1. https://chrome.google.com/webstore/devconsole — upload the .zip"
echo "  2. Or for local testing: chrome://extensions → Load unpacked → pick the unzipped folder above"
