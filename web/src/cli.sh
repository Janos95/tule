#!/bin/sh
# Installs the tule CLI and signs it in:
#
#   curl -fsSL __TULE_URL__/cli.sh | sh -s -- tulel_…
#
# Agents get this command, with a one-time code, from the connector's connect_info,
# so the user does nothing. Without a code, `tule login` shows a link to approve.
# Running it again is safe.
set -eu

URL="__TULE_URL__"
WHEEL="__WHEEL__"
CODE="${1:-}"
SRC="$URL/install/$WHEEL"

say() { printf '\033[1mtule:\033[0m %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# the package first, checked: a server without it answers with a web page
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
WHL="$TMPD/$(basename "$WHEEL")"
curl -fsSL -o "$WHL" "$SRC" || die "could not download the CLI package from $SRC"
[ "$(head -c 2 "$WHL")" = "PK" ] || die "$URL has no CLI package at install/$WHEEL (it answered with a web page): the server needs a restart or redeploy"
SRC="file://$WHL"

# uv if it is there (it brings its own Python), else pip, else install uv
TULE=""
if have uv || [ -x "$HOME/.local/bin/uv" ]; then
  UV=$(command -v uv || echo "$HOME/.local/bin/uv")
elif have python3 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null && python3 -m pip --version >/dev/null 2>&1; then
  UV=""
  say "installing the tule CLI with pip"
  python3 -m pip install --quiet --upgrade "$WHL" 2>/dev/null \
    || python3 -m pip install --quiet --upgrade --user "$WHL" 2>/dev/null \
    || python3 -m pip install --quiet --upgrade --user --break-system-packages "$WHL" \
    || die "pip could not install $SRC"
  TULE=$(command -v tule || echo "$(python3 -m site --user-base)/bin/tule")
else
  say "installing uv (Python packages)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "could not install uv; install Python 3.10+ with pip and run this again"
  UV="$HOME/.local/bin/uv"
fi
if [ -n "$UV" ]; then
  say "installing the tule CLI"
  "$UV" tool install --quiet --force --python 3.13 "tule-cli @ $SRC"
  TULE="$("$UV" tool dir --bin)/tule"
fi
[ -x "$TULE" ] || die "installed, but cannot find the tule command (looked for $TULE)"
if [ "$(command -v tule 2>/dev/null)" = "$TULE" ]; then say "installed: $TULE"; else say "installed: $TULE (not on PATH: add $(dirname "$TULE") to it, or run it by this path)"; fi

rm -rf "$TMPD"
if [ -n "$CODE" ]; then
  exec "$TULE" login --url "$URL" --code "$CODE"
else
  exec "$TULE" login --url "$URL"
fi
