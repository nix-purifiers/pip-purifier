# SPDX-License-Identifier: MIT
# Materialize a clean Git-aware tree and its inputs, excluding generated evidence.
source_snapshot() {
  if ! git diff --quiet HEAD; then
    echo 'commit tracked changes before capturing runtime evidence' >&2
    return 1
  fi
  local metadata archive
  metadata=$(nix flake metadata --no-update-lock-file --json "$PWD") || return
  archive=$(nix flake archive --no-update-lock-file --json "$PWD") || return
  if [ -n "${PIP_PURIFIER_EVIDENCE_DIR:-}" ]; then
    printf '%s\n' "$metadata" > "$PIP_PURIFIER_EVIDENCE_DIR/source.json"
    printf '%s\n' "$archive" > "$PIP_PURIFIER_EVIDENCE_DIR/source-archive.json"
  fi
  python3 - "$metadata" "$archive" <<'PYTHON'
import json, sys
from pathlib import Path
metadata, archive = map(json.loads, sys.argv[1:])
assert metadata['path'] == archive['path'], 'source changed during capture'
def physical(node):
    path = Path(node['path'])
    assert path.is_dir() and path.resolve(strict=True) == path, path
    for child in node.get('inputs', {}).values():
        physical(child)
physical(archive)
assert (Path(archive['path']) / 'flake.nix').is_file()
print(archive['path'])
PYTHON
}
