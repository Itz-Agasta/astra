#!/usr/bin/env bash
# Refresh Stellarium Web Engine artifacts (AGPL-3.0).
# Glue + wasm come from a public matching pair; skydata from the same tree.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
dest="$root/public/stel"
mkdir -p "$dest"
curl -fsSL -o "$dest/stellarium-web-engine.js" \
  "https://raw.githubusercontent.com/Russell0014/stellarium-stile/main/public/stellarium-web-engine.js"
curl -fsSL -o "$dest/stellarium-web-engine.wasm" \
  "https://raw.githubusercontent.com/Russell0014/stellarium-stile/main/public/stellarium-web-engine.wasm"
curl -fsSL -o "$dest/LICENSE-AGPL-3.0.txt" \
  "https://raw.githubusercontent.com/Stellarium/stellarium-web-engine/master/LICENSE-AGPL-3.0.txt"
echo "wrote $dest"
