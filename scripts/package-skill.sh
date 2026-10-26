#!/usr/bin/env bash
# Package the agent skill as dist/cross-agent-workspace-skill.zip (for uploading to Claude).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
cd "$ROOT_DIR"
command -v zip >/dev/null 2>&1 || die "zip is not installed (apt install zip)."
mkdir -p dist
rm -f dist/cross-agent-workspace-skill.zip
(cd skills && zip -qr ../dist/cross-agent-workspace-skill.zip cross-agent-workspace -x '*/__pycache__/*')
info "Created dist/cross-agent-workspace-skill.zip"
