#!/usr/bin/env bash
# Downloads the Enterprise-Bench "Maple Payments" dataset into devrev-mcp/data (gitignored — do not commit it).
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
git clone --depth 1 https://github.com/devrev/enterprise-bench.git "$tmp/eb"
mkdir -p data
unzip -oq "$tmp/eb/artifacts/data.zip" -d "$tmp/x"
# the zip may or may not have a top-level folder; find the directory that holds crm_json_data
src=$(dirname "$(find "$tmp/x" -type d -name crm_json_data | head -1)")
cp -r "$src"/. data/
rm -rf "$tmp"
echo "✓ data ready: $(ls data | tr '\n' ' ')"
