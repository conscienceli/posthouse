#!/usr/bin/env bash
# Package the agent skill as dist/posthouse-skill.zip (for uploading to Claude).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
cd "$ROOT_DIR"
command -v zip >/dev/null 2>&1 || die "zip is not installed (apt install zip)."
mkdir -p dist
rm -f dist/posthouse-skill.zip
(cd skills && zip -qr ../dist/posthouse-skill.zip posthouse -x '*/__pycache__/*')
info "Created dist/posthouse-skill.zip"
