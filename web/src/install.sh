#!/bin/sh
# Adds this computer to your Tule account, so your projects' machines can run on it.
#
#   curl -fsSL __TULE_URL__/install.sh | sh -s -- tulep_…
#
# (the command with your one-time key is in Settings → Your computers.) It
#   1. installs Docker if it is missing: Docker Engine on Linux (plus NVIDIA's
#      container toolkit when there is an NVIDIA GPU), OrbStack on macOS;
#   2. installs the tule CLI with uv (which brings its own Python);
#   3. runs `tule host up`: adds the computer with the key and starts the
#      background service (a systemd user unit, or a LaunchAgent on macOS).
# Running it again is safe. Windows: run it inside WSL 2.
set -eu

URL="__TULE_URL__"
WHEEL="__WHEEL__"
KEY="${1:-}"

say() { printf '\033[1mtule:\033[0m %s\n' "$*"; }
die() { printf '\033[1mtule:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

case "$(uname -s)" in
Darwin)
  ORB=/Applications/OrbStack.app/Contents/MacOS/bin/orb
  if ! have docker && [ ! -d /Applications/OrbStack.app ] && [ ! -d /Applications/Docker.app ]; then
    say "installing OrbStack (Docker for macOS)"
    if have brew; then
      brew install --cask orbstack >/dev/null
    else
      arch=$(uname -m); [ "$arch" = x86_64 ] && arch=amd64
      tmp=$(mktemp -d)
      curl -fsSL -o "$tmp/OrbStack.dmg" "https://orbstack.dev/download/stable/latest/$arch"
      hdiutil attach -nobrowse -quiet -mountpoint "$tmp/mnt" "$tmp/OrbStack.dmg"
      cp -R "$tmp/mnt/OrbStack.app" /Applications/
      hdiutil detach -quiet "$tmp/mnt"
      rm -rf "$tmp"
    fi
    "$ORB" start >/dev/null
    # machines get the whole computer: all cores (OrbStack's default) and all but 2 GB of memory
    mem=$(( $(sysctl -n hw.memsize) / 1048576 - 2048 ))
    "$ORB" config set memory_mib "$mem" >/dev/null && "$ORB" stop >/dev/null && "$ORB" start >/dev/null
    say "OrbStack is running (if it shows a welcome window, choose Docker or close it)"
  elif [ -d /Applications/OrbStack.app ]; then
    "$ORB" start >/dev/null 2>&1 || true
  elif [ -d /Applications/Docker.app ] && ! docker info >/dev/null 2>&1; then
    say "starting Docker Desktop"
    open -ga Docker
    i=0; until docker info >/dev/null 2>&1 || [ $i -ge 60 ]; do sleep 2; i=$((i + 1)); done
  fi
  ;;
Linux)
  if ! have docker; then
    say "installing Docker Engine (asks for your password)"
    curl -fsSL https://get.docker.com | $SUDO sh >/dev/null
    $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
    [ -n "$SUDO" ] && $SUDO usermod -aG docker "$(id -un)"
  fi
  if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1 && ! $SUDO docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
    say "installing NVIDIA's container toolkit, so machines here get the GPU"
    if have apt-get; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | $SUDO gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        | $SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
      $SUDO apt-get update -qq && $SUDO apt-get install -y -qq nvidia-container-toolkit >/dev/null
    elif have dnf; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | $SUDO tee /etc/yum.repos.d/nvidia-container-toolkit.repo >/dev/null
      $SUDO dnf install -y -q nvidia-container-toolkit
    else
      say "install NVIDIA's container toolkit yourself for GPU machines: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
    fi
    if have nvidia-ctk; then
      $SUDO nvidia-ctk runtime configure --runtime=docker >/dev/null && $SUDO systemctl restart docker
    fi
  fi
  # the service keeps running after you log out, and starts at boot
  if have loginctl && [ -n "$SUDO" ]; then $SUDO loginctl enable-linger "$(id -un)" 2>/dev/null || true; fi
  ;;
*) die "this script is for Linux and macOS; on Windows, run it inside WSL 2" ;;
esac

if ! have uv && [ ! -x "$HOME/.local/bin/uv" ]; then
  say "installing uv (Python packages)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
fi
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
say "installing the tule CLI"
"$UV" tool install --quiet --force --python 3.13 "tule-cli @ $URL/install/$WHEEL"
TULE="$("$UV" tool dir --bin)/tule"

if [ -n "$KEY" ]; then
  exec "$TULE" host up --url "$URL" --key "$KEY"
else
  exec "$TULE" host up --url "$URL" --no-browser
fi
