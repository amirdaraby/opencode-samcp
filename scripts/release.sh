#!/usr/bin/env bash
# Usage: scripts/release.sh 0.0.4
# Bumps the version in pyproject.toml, package.json and uv.lock, commits, pushes main,
# then tags vX.Y.Z and pushes the tag, which triggers the publish workflow (npm + PyPI).
set -euo pipefail
cd "$(dirname "$0")/.."

V="${1:-}"
[[ "$V" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 X.Y.Z" >&2; exit 1; }
[[ "$(git branch --show-current)" == "main" ]] || { echo "must be on main" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean" >&2; exit 1; }
git rev-parse "v$V" >/dev/null 2>&1 && { echo "tag v$V already exists" >&2; exit 1; }
git ls-remote --exit-code --tags origin "v$V" >/dev/null 2>&1 && { echo "tag v$V already exists on origin" >&2; exit 1; }

sed -i -E "s/^version = \"[^\"]+\"/version = \"$V\"/" pyproject.toml
sed -i -E "0,/\"version\": \"[^\"]+\"/s//\"version\": \"$V\"/" package.json
uv lock -q
uv run --group dev pytest -q

git commit -am "Release $V"
git push origin main
git tag "v$V"
git push origin "v$V"
echo "Tagged v$V. Watch the publish run in GitHub Actions."
