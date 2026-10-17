#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

NO_RESTART=0
if [ "${1:-}" = "--no-restart" ]; then NO_RESTART=1; shift; fi

ping -c1 -W2 github.com >/dev/null 2>&1 || { echo "no internet - connect to WiFi first" >&2; exit 1; }
git fetch --tags --force --prune origin
target="${1:-}"
if [ -z "$target" ]; then
  target=$(git tag --sort=-v:refname | head -1)
  [ -z "$target" ] && target="origin/main"
fi
echo "updating to $target"
git reset --hard "$target"
echo "done: $(git describe --tags --always)"

[ "$NO_RESTART" = "1" ] || sudo -n systemctl restart spooled-server 2>/dev/null \
  || echo "restart spooled-server to apply"
