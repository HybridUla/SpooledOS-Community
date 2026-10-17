#!/usr/bin/env bash
set -u

URL="${SPOOLED_URL:-http://localhost/}"
PROFILE="$HOME/.config/chromium"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

BROWSER=chromium
command -v chromium >/dev/null 2>&1 || BROWSER=chromium-browser

mkdir -p "$HOME/.config/labwc"
cat > "$HOME/.config/labwc/menu.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu>
  <menu id="root-menu" label=""></menu>
  <menu id="client-menu" label=""></menu>
</openbox_menu>
XML

rm -f "$PROFILE"/Singleton* 2>/dev/null || true

mark(){ logger -t spooled-kiosk "$(cut -d' ' -f1 /proc/uptime)s $*" 2>/dev/null || true; }
mark "session up, kiosk starting"

chrome(){
  mark "launching chromium: $1"
  "$BROWSER" \
    --ozone-platform=wayland \
    --kiosk --incognito --noerrdialogs --disable-infobars \
    --password-store=basic \
    --touch-events=enabled \
    --overscroll-history-navigation=0 \
    --no-first-run --no-default-browser-check \
    --disable-sync --disable-extensions --disable-default-apps \
    --disable-component-update --disable-background-networking \
    --disable-breakpad --disable-domain-reliability \
    --disable-translate \
    --disable-features=TranslateUI,MediaRouter,OptimizationHints,AutofillServerCommunication \
    --check-for-update-interval=31536000 \
    "$1"
}

up(){ curl -sf -o /dev/null --max-time 2 "${URL%/}/api/version"; }

BOOT_PORT=8730
if ! up; then
  BOOT_DIR="$(mktemp -d)"
  cp "$APP_DIR/boot.html" "$BOOT_DIR/index.html" 2>/dev/null || true
  python3 -m http.server "$BOOT_PORT" --bind 127.0.0.1 --directory "$BOOT_DIR" \
    >/dev/null 2>&1 &
  SRV_PID=$!
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:$BOOT_PORT/" && break
    sleep 0.2
  done
  chrome "http://127.0.0.1:$BOOT_PORT/" &
  BOOT_PID=$!

  while ! up; do
    kill -0 "$BOOT_PID" 2>/dev/null || break
    sleep 0.5
  done

  kill "$BOOT_PID" 2>/dev/null || true
  wait "$BOOT_PID" 2>/dev/null || true
  kill "$SRV_PID" 2>/dev/null || true
  rm -rf "$BOOT_DIR" 2>/dev/null || true
  rm -f "$PROFILE"/Singleton* 2>/dev/null || true
fi

while true; do
  chrome "$URL"
  rm -f "$PROFILE"/Singleton* 2>/dev/null || true
  sleep 2
done
