#!/bin/bash
# Mirrors the marketplace's automated checks plus a few of our own.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok   %s\n' "$*"; }

jq -e . manifest.json >/dev/null || fail "manifest.json is not valid JSON"
ok "manifest parses"

for key in schemaVersion id name version author license description kinds entryPoints; do
  jq -e --arg k "$key" 'has($k)' manifest.json >/dev/null || fail "manifest missing $key"
done
ok "required manifest fields present"

id="$(jq -r .id manifest.json)"
[[ "$id" != omarchy.* ]] || fail "third-party plugins may not use the omarchy.* prefix"
ok "id $id"

# every kind maps to an entry point and the file exists
while IFS= read -r kind; do
  case "$kind" in
    service) key=service ;; bar-widget) key=barWidget ;; panel) key=panel ;;
    overlay) key=overlay ;; menu) key=menu ;; bar) key=bar ;;
    *) fail "unknown kind $kind" ;;
  esac
  file="$(jq -r --arg k "$key" '.entryPoints[$k] // empty' manifest.json)"
  [[ -n "$file" ]] || fail "kind $kind has no entryPoints.$key"
  [[ -f "$file" ]] || fail "entry point $file for $kind does not exist"
  ok "kind $kind -> $file"
done < <(jq -r '.kinds[]' manifest.json)

[[ -f README.md ]] || fail "README.md missing"
[[ -f LICENSE ]] || fail "LICENSE missing"
ok "README and LICENSE present"

if find . -path ./.git -prune -o -type l -print | grep -q .; then
  fail "symlinks are not allowed in a plugin folder"
fi
ok "no symlinks"

[[ -x sonomarchy-backend ]] || fail "sonomarchy-backend is not executable"
bash -n sonomarchy-backend || fail "sonomarchy-backend has a syntax error"
python3 -m py_compile sonomarchy.py || fail "sonomarchy.py does not compile"
ok "backend scripts compile"

grep -q "^$(jq -r .version manifest.json)" <(grep -oE '^## [0-9]+\.[0-9]+\.[0-9]+' CHANGELOG.md | sed 's/^## //') \
  || fail "CHANGELOG.md has no entry for version $(jq -r .version manifest.json)"
ok "changelog covers $(jq -r .version manifest.json)"

if command -v qmllint >/dev/null 2>&1; then
  qmllint Service.qml >/dev/null 2>&1 && ok "qmllint Service.qml" || printf 'warn qmllint reported issues (often import resolution; check manually)\n'
fi

echo "all checks passed"
