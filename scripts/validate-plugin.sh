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
# -B, and a throwaway cache dir: py_compile writes __pycache__ into the plugin
# directory otherwise, and the shell reloads the plugin when that directory
# changes -- which restarts the backend (see FIX 15 and sonomarchy-backend).
PYTHONPYCACHEPREFIX="$(mktemp -d)" python3 -B -m py_compile sonomarchy.py \
  || fail "sonomarchy.py does not compile"
ok "backend scripts compile"

grep -q "^$(jq -r .version manifest.json)" <(grep -oE '^## [0-9]+\.[0-9]+\.[0-9]+' CHANGELOG.md | sed 's/^## //') \
  || fail "CHANGELOG.md has no entry for version $(jq -r .version manifest.json)"
ok "changelog covers $(jq -r .version manifest.json)"

# Qt ships these in a libexec-ish directory that is not on PATH on Arch, so a
# bare `command -v` silently skipped both checks here for their whole life.
find_qt_tool() {
  local name="$1" candidate
  for candidate in "$(command -v "$name" 2>/dev/null || true)" \
                   "/usr/lib/qt6/$name" "/usr/lib64/qt6/$name" \
                   "/usr/lib/qt6/bin/$name" "/usr/lib64/qt6/bin/$name"; do
    [[ -n "$candidate" && -x "$candidate" ]] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

if qmllint="$(find_qt_tool qmllint)"; then
  "$qmllint" Service.qml >/dev/null 2>&1 && ok "qmllint Service.qml" || printf 'warn qmllint reported issues (often import resolution; check manually)\n'
fi

# qmllint is advisory and NOT enough on its own: it exits 0 on "Property value
# set multiple times", which is a load-time failure that takes the whole
# service out -- the shell logs "service plugin load failed" and every Sonos
# output disappears. Shipped exactly that on 2026-09-08 (two
# Component.onDestruction blocks in one file) with the validator green, and
# only noticed because the zones vanished. qmlcachegen runs the real QML
# compiler, so it is the gate; qmllint stays as the style pass.
if qmlcachegen="$(find_qt_tool qmlcachegen)"; then
  scratch="$(mktemp -d)"
  trap 'rm -rf "$scratch"' EXIT
  for qml in *.qml; do
    [[ -e "$qml" ]] || continue
    # --resource-path is required or the compiler refuses the file outright,
    # and its value only has to be a plausible qrc path.
    "$qmlcachegen" --resource-path "/$qml" -o "$scratch/${qml%.qml}.cpp" "$qml" \
      || fail "$qml does not compile (the shell would refuse to load it)"
    ok "$qml compiles"
  done
else
  printf 'warn qmlcachegen not found; QML is unchecked by the compiler\n'
fi

echo "all checks passed"
