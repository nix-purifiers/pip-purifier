# SPDX-License-Identifier: MIT
# Capture a clean, Git-aware source tree: generated evidence is untracked.
source_snapshot() {
  if ! git diff --quiet HEAD; then
    echo 'commit tracked changes before capturing runtime evidence' >&2
    return 1
  fi
  local metadata
  metadata=$(nix flake metadata --no-write-lock-file --json "$PWD") || return
  if [ -n "${PIP_PURIFIER_EVIDENCE_DIR:-}" ]; then
    printf '%s\n' "$metadata" > "$PIP_PURIFIER_EVIDENCE_DIR/source.json"
  fi
  printf '%s\n' "$metadata" | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])'
}
