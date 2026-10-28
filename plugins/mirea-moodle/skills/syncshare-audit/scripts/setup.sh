#!/usr/bin/env bash
# Prepare the local SyncShare audit harness:
#   - download and unpack the Chrome extension
#   - install playwright into the audit directory
#   - find (or install) a Chrome that supports extensions
#   - create a self-signed TLS certificate for the syncshare.ru mock
#
# Idempotent: safe to re-run.
set -euo pipefail

SKILL_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
AUDIT_DIR=${SYNC_AUDIT_DIR:-$HOME/.syncshare-audit}
MIREA_DIR=${MIREA_DIR:-$HOME/.mirea}
HARNESS="$AUDIT_DIR/harness"
EXT="$AUDIT_DIR/extension"
CRX="$AUDIT_DIR/syncshare.crx"

EXT_ID=lngijbnmdkejbgnkakeiapeppbpaapib
KNOWN_SHA=2a91e8ff6335166e45a87a4300785c467f601de7a3c44bd93804280b1a2b7aee

say() { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

need curl
need unzip
need node
need npm
need python3

mkdir -p "$AUDIT_DIR" "$HARNESS" "$EXT"

# --- harness sources ------------------------------------------------------- #
cp -f "$SKILL_DIR/scripts/harness/server.js" "$HARNESS/server.js"
cp -f "$SKILL_DIR/scripts/harness/runner.js" "$HARNESS/runner.js"
cp -f "$SKILL_DIR/scripts/harness/package.json" "$HARNESS/package.json"

# --- extension ------------------------------------------------------------- #
if [ ! -f "$EXT/manifest.json" ]; then
  say "downloading SyncShare extension ($EXT_ID) ..."
  curl --noproxy '*' -4 -sSL --max-time 120 -o "$CRX" \
    "https://clients2.google.com/service/update2/crx?response=redirect&prodversion=130.0&acceptformat=crx2,crx3&x=id%3D${EXT_ID}%26uc" \
    || die "CRX download failed (network / Chrome Web Store)"
  unzip -o -q "$CRX" -d "$EXT" || true   # CRX header makes unzip return 1
  [ -f "$EXT/manifest.json" ] || die "CRX unpack failed: no manifest.json in $EXT"
fi

if [ -f "$CRX" ]; then
  got=$(sha256sum "$CRX" | awk '{print $1}')
  if [ "$got" != "$KNOWN_SHA" ]; then
    warn "extension SHA-256 is $got"
    warn "reviewed build 2.19.0 was $KNOWN_SHA"
    warn "a different version may change questionIdentity; re-validate before trusting results"
  fi
fi

# SyncShare 2.19.0 asks the background for settings during content-script
# init. If the background has not bound the get-settings topic yet, the bus
# returns null and the content script crashes before parsing the page. Fall
# back to telemetry-off defaults when that happens; the questionIdentity code
# is untouched.
COMMONS="$EXT/js/commons.js"
if [ -f "$COMMONS" ]; then
  if python3 - "$COMMONS" <<'PY'
import sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()
old = 't.getSettings=async function(e,t){const n=await e.publishToBackground(r.Topics.GetSettings);return t.debug("Got settings",(0,s.extras)({settings:n})),n}'
new = 't.getSettings=async function(e,t){try{const o=await e.publishToBackground(r.Topics.GetSettings);if(o&&"allowTechnicalDataCollection"in o)return t.debug("Got settings",(0,s.extras)({settings:o})),o}catch{}return{widgetToggleHotkey:"esc",widgetToggleDoubleClick:!0,hideWidgetByDefault:!1,hideNotifications:!1,allowTechnicalDataCollection:!1,privacyPolicyAcceptedByUser:!1,aiWebSearch:!1}}'

if new in text and old not in text:
    print("already")
elif old in text:
    open(path, "w", encoding="utf-8").write(text.replace(old, new, 1))
    print("patched")
else:
    sys.exit(3)
PY
  then
    say "extension: settings fallback ready"
  else
    warn "could not patch the extension settings fallback; the harness will still retry whole pages"
  fi
fi

EXT_VERSION=$(node -e "console.log(require('$EXT/manifest.json').version)" 2>/dev/null || echo '?')
say "extension: $EXT_VERSION"

# --- node deps ------------------------------------------------------------- #
if [ ! -d "$HARNESS/node_modules/playwright" ]; then
  say "installing playwright ..."
  (cd "$HARNESS" && npm install --no-audit --no-fund) \
    || die "npm install failed in $HARNESS"
fi
(cd "$HARNESS" && node -e "require('playwright')") \
  || die "playwright is not importable from $HARNESS"

# --- browser --------------------------------------------------------------- #
CHROME_PATH=${CHROME_PATH:-}
if [ -z "$CHROME_PATH" ]; then
  for c in google-chrome-stable google-chrome chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1; then
      CHROME_PATH=$(command -v "$c")
      break
    fi
  done
fi
if [ -z "$CHROME_PATH" ]; then
  CHROME_PATH=$(find "$AUDIT_DIR/browsers" -type f -name chrome -path '*chrome-linux64*' 2>/dev/null | head -1 || true)
fi
if [ -z "$CHROME_PATH" ]; then
  say "installing Chrome for Testing ..."
  npx --yes @puppeteer/browsers install chrome@stable --path "$AUDIT_DIR/browsers" >/dev/null 2>&1 \
    || die "could not install Chrome; set CHROME_PATH to a Chrome/Chromium binary"
  CHROME_PATH=$(find "$AUDIT_DIR/browsers" -type f -name chrome -path '*chrome-linux64*' 2>/dev/null | head -1 || true)
fi
[ -n "$CHROME_PATH" ] && [ -x "$CHROME_PATH" ] || die "no executable Chrome found (set CHROME_PATH)"
say "chrome: $CHROME_PATH"

# --- TLS cert for the syncshare.ru mock ------------------------------------ #
need openssl
if [ ! -s "$HARNESS/cert.pem" ] || [ ! -s "$HARNESS/key.pem" ]; then
  say "generating self-signed certificate for syncshare.ru ..."
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$HARNESS/key.pem" -out "$HARNESS/cert.pem" \
    -days 2 -subj "/CN=syncshare.ru" >/dev/null 2>&1 \
    || die "openssl failed"
fi

# --- persisted environment ------------------------------------------------- #
cat > "$AUDIT_DIR/env.json" <<EOF
{
  "CHROME_PATH": "$CHROME_PATH",
  "SYNC_EXT_DIR": "$EXT",
  "MIREA_DIR": "$MIREA_DIR"
}
EOF

cat > "$AUDIT_DIR/env.sh" <<EOF
export SYNC_AUDIT_DIR='$AUDIT_DIR'
export CHROME_PATH='$CHROME_PATH'
export SYNC_EXT_DIR='$EXT'
export MIREA_DIR='$MIREA_DIR'
EOF

if [ -z "${DISPLAY:-}" ] && ! command -v xvfb-run >/dev/null 2>&1; then
  warn "no DISPLAY and no xvfb-run: headed Chrome will not start"
  warn "install xvfb or run on a desktop session"
fi

say ""
say "OK: harness ready"
say "state:   $AUDIT_DIR"
say "load env: source $AUDIT_DIR/env.sh"
