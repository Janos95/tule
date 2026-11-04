#!/bin/sh
# A machine for a project on the local dev server (npm --prefix control run dev), in Docker, running this
# checkout's code: python/tule, cli/tule_cli, runtime/*.py and skills/ are mounted over the image's copies, so a
# change shows on the next build without rebuilding the image. Restart the machine to pick up agent changes.
#
#   tools/dev-machine.sh PROJECT          start (or restart) it; tule admin pull PROJECT brings one from production
#   docker rm -f tule-dev-PROJECT          stop it
set -eu
p=${1:?usage: tools/dev-machine.sh PROJECT}
repo=$(cd "$(dirname "$0")/.." && pwd)
image=${TULE_DEV_IMAGE:-tule-runtime:dev}  # docker build -f runtime/Dockerfile -t tule-runtime:dev . (new dependencies only)
token=$(grep '^DEV_TOKEN=' "$repo/control/.dev.vars" | cut -d= -f2-)
home=$(mktemp -d)
env=$(cd "$repo/cli" && HOME=$home TULE_URL=http://127.0.0.1:8787 TULE_TOKEN=$token PYTHONPATH=. python3 -m tule_cli join -p "$p" | grep TULE_JOIN_TOKEN)
rm -rf "$home"
site=/usr/local/lib/python3.13/site-packages
mounts="-v $repo/python/tule:$site/tule:ro -v $repo/cli/tule_cli:$site/tule_cli:ro -v $repo/skills:/opt/tule/skills:ro"
for f in "$repo"/runtime/*.py; do mounts="$mounts -v $f:/opt/tule/$(basename "$f"):ro"; done
docker rm -f "tule-dev-$p" >/dev/null 2>&1 || true
# shellcheck disable=SC2086
docker run -d --name "tule-dev-$p" --add-host host.docker.internal:host-gateway $mounts \
  $(for kv in $env; do printf -- '-e %s ' "$kv"; done) "$image" >/dev/null
echo "tule-dev-$p is starting; open http://127.0.0.1:8787/p/$p (sign in: http://127.0.0.1:8787/auth/dev)"
