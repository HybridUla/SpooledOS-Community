#!/usr/bin/env bash
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/boot/spooled"
DEST=/usr/share/plymouth/themes/spooled

echo "==> Installing plymouth…"
sudo apt-get update
sudo apt-get install -y plymouth plymouth-themes

echo "==> Installing the Spooled theme…"
sudo mkdir -p "$DEST"
sudo cp "$SRC/spooled.plymouth" "$SRC/spooled.script" \
        "$SRC/spooled-wheel.png" "$SRC/spooled-word.png" "$DEST/"

echo "==> Setting Spooled as the default boot theme…"
sudo plymouth-set-default-theme -R spooled

BOOTDIR=/boot/firmware; [ -d "$BOOTDIR" ] || BOOTDIR=/boot
CMDLINE="$BOOTDIR/cmdline.txt"
CONFIG="$BOOTDIR/config.txt"

echo "==> Quieting boot ($CMDLINE)…"
sudo sed -i 's/\bconsole=tty1\b/console=tty3/' "$CMDLINE"
for f in quiet splash plymouth.ignore-serial-consoles logo.nologo vt.global_cursor_default=0 loglevel=0; do
  grep -qw "$f" "$CMDLINE" || sudo sed -i "s/\$/ $f/" "$CMDLINE"
done

ROT="${SPLASH_ROTATE:-270}"
if grep -q 'video=DSI-1:' "$CMDLINE"; then
  sudo sed -i "s/video=DSI-1:[^ ]*/video=DSI-1:480x1920,rotate=$ROT/" "$CMDLINE"
else
  sudo sed -i "s/\$/ video=DSI-1:480x1920,rotate=$ROT/" "$CMDLINE"
fi

sudo sed -i 's/  */ /g' "$CMDLINE"

echo "==> Disabling the rainbow splash ($CONFIG)…"
grep -q '^disable_splash=1' "$CONFIG" 2>/dev/null || \
  echo 'disable_splash=1' | sudo tee -a "$CONFIG" >/dev/null

echo
echo "============================================================"
echo " Spooled boot splash installed. Reboot to see it: sudo reboot"
echo "============================================================"
