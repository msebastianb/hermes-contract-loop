#!/usr/bin/env bash
# Installs the hermes-contract-loop skill into Hermes.
#   ./install.sh              # ${HERMES_HOME:-~/.hermes}/skills/software-development/hermes-contract-loop
#   ./install.sh --dir <path> # custom skills root
set -euo pipefail

SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/skills/hermes-contract-loop"
ROOT="${HERMES_HOME:-$HOME/.hermes}/skills/software-development"
if [ "${1:-}" = "--dir" ]; then
  [ -n "${2:-}" ] || { echo "error: --dir requires a path" >&2; exit 2; }
  ROOT="$2"
fi

DEST="$ROOT/hermes-contract-loop"
mkdir -p "$ROOT"
rm -rf "$DEST"
cp -R "$SRC" "$DEST"
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "installed: $DEST"
