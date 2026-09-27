#!/bin/zsh
# launchd entry point for the WHOOP -> ActivityWatch sync.
# Secrets come only from the macOS Keychain; the repo path is configurable because
# a hard-coded path broke the sync for two months when the checkout moved.
set -euo pipefail
AW_WHOOP_DIR="${AW_WHOOP_DIR:-$HOME/Documents/Playground/aw-importer-whoop}"
CLIENT_ID="$(/usr/bin/security find-generic-password -a "$USER" -s aw-importer-whoop-client-id -w)"
CLIENT_SECRET="$(/usr/bin/security find-generic-password -a "$USER" -s aw-importer-whoop-client-secret -w)"
if [[ ! -x "$AW_WHOOP_DIR/.venv/bin/aw-importer-whoop" ]]; then
  echo "aw-importer-whoop not found in $AW_WHOOP_DIR (set AW_WHOOP_DIR or create .venv)" >&2
  exit 78  # EX_CONFIG: launchd shows a distinct exit code instead of a silent loop
fi
cd "$AW_WHOOP_DIR"
exec .venv/bin/aw-importer-whoop sync --client-id "$CLIENT_ID" --client-secret "$CLIENT_SECRET" --interval "${AW_WHOOP_INTERVAL:-3600}" "$@"
