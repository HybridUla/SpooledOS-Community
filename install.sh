#!/usr/bin/env bash
# SpooledOS Community is free software under the GNU GPL v3 - see LICENSE.
set -e

REPO_URL="${REPO_URL:-https://github.com/HybridUla/SpooledOS-Community.git}"
SSID="${SSID:-Spooled}"
PASS="${PASS:-SpooledDash}"
IFACE="${IFACE:-wlan0}"
PORT=80
USER_NAME="$(whoami)"

say(){ printf '\n==> %s\n' "$*"; }
die(){ printf '\n!!  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" != "0" ] || die "Run this as your normal user, not with sudo - it asks for sudo itself."
command -v apt-get >/dev/null || die "This needs Raspberry Pi OS (it uses apt)."
[ -d /etc/xdg/labwc ] || [ -e /usr/bin/labwc ] || \
  echo "    note: labwc not found yet - it will be installed. If this card is Raspberry Pi OS Lite, start again with the desktop version."
[ ${#PASS} -ge 8 ] && [ ${#PASS} -le 63 ] || die "PASS must be 8 to 63 characters."
sudo -v || die "sudo is needed to set up the screen and services."

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/server.py" ] && [ -f "$SELF_DIR/index.html" ]; then
  APP_DIR="$SELF_DIR"
else
  APP_DIR="${DIR:-$HOME/spooledos}"
fi

echo "SpooledOS Community installer"
echo "    folder : $APP_DIR"
echo "    user   : $USER_NAME"
echo "    DS1 WiFi network the dash will make: $SSID"

say "Installing packages (a few minutes on a fresh card)…"
sudo apt-get update -qq
sudo apt-get install -y -qq --no-install-recommends \
  labwc swaybg wlr-randr python3 network-manager git curl avahi-daemon
sudo apt-get install -y -qq chromium 2>/dev/null || sudo apt-get install -y -qq chromium-browser

if [ ! -d "$APP_DIR/.git" ]; then
  say "Downloading SpooledOS Community…"
  git clone -q "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
git fetch -q --tags --force origin 2>/dev/null || true
LATEST="$(git tag --sort=-v:refname | head -1)"
if [ -n "$LATEST" ]; then git -c advice.detachedHead=false checkout -q "$LATEST"; fi
echo "    version: $(git describe --tags --always 2>/dev/null || echo unknown)"
chmod +x "$APP_DIR"/*.sh

say "Setting up the 8.8\" screen…"
BOOTCFG=/boot/firmware/config.txt; [ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
OVERLAY="dtoverlay=vc4-kms-dsi-waveshare-panel-v2,8_8_inch_a"
sudo sed -i '/vc4-kms-dsi-waveshare-panel,8_8/d' "$BOOTCFG"
grep -qF "$OVERLAY" "$BOOTCFG" || echo "$OVERLAY" | sudo tee -a "$BOOTCFG" >/dev/null
grep -q '^dtoverlay=vc4-kms-v3d' "$BOOTCFG" || echo 'dtoverlay=vc4-kms-v3d' | sudo tee -a "$BOOTCFG" >/dev/null

sudo tee /etc/udev/rules.d/90-spooled-backlight.rules >/dev/null <<'EOF'
SUBSYSTEM=="backlight", ACTION=="add", RUN+="/bin/chgrp video /sys/class/backlight/%k/brightness", RUN+="/bin/chmod g+w /sys/class/backlight/%k/brightness"
EOF
sudo usermod -aG video "$USER_NAME"
for f in /sys/class/backlight/*/brightness; do
  [ -e "$f" ] && { sudo chgrp video "$f"; sudo chmod g+w "$f"; } || true
done

if [ "${RTC:-0}" = "1" ]; then
  say "Enabling the DS3231 clock chip…"
  sudo raspi-config nonint do_i2c 0 || true
  grep -q '^dtoverlay=i2c-rtc,ds3231' "$BOOTCFG" || \
    echo 'dtoverlay=i2c-rtc,ds3231' | sudo tee -a "$BOOTCFG" >/dev/null
  sudo apt-get install -y -qq --no-install-recommends i2c-tools
  sudo apt-get remove -y -qq fake-hwclock 2>/dev/null || true
  sudo update-rc.d -f fake-hwclock remove 2>/dev/null || true
  [ -f /lib/udev/hwclock-set ] && sudo sed -i \
    '/if \[ -e \/run\/systemd\/system \]/,+2 s/^/#/' /lib/udev/hwclock-set
fi

say "Installing the SpooledOS service…"
sudo tee /etc/systemd/system/spooled-server.service >/dev/null <<EOF
[Unit]
Description=SpooledOS Community
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $APP_DIR/server.py --port $PORT
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo sed -i 's/^#\?host-name=.*/host-name=spooled/' /etc/avahi/avahi-daemon.conf 2>/dev/null || true
sudo systemctl enable avahi-daemon >/dev/null 2>&1 || true
sudo systemctl restart avahi-daemon 2>/dev/null || true

say "Creating the '$SSID' WiFi network for the DS1…"
sudo systemctl enable NetworkManager >/dev/null 2>&1
sudo systemctl start NetworkManager
if ! nmcli -t -f NAME connection show | grep -qx "spooled-ap"; then
  sudo nmcli connection add type wifi ifname "$IFACE" con-name spooled-ap \
    autoconnect yes ssid "$SSID" >/dev/null
fi
sudo nmcli connection modify spooled-ap \
  802-11-wireless.ssid "$SSID" 802-11-wireless.mode ap 802-11-wireless.band bg \
  ipv4.method shared wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" \
  connection.autoconnect-priority 10 802-11-wireless.powersave 2
sudo tee /etc/NetworkManager/conf.d/spooled-wifi-powersave.conf >/dev/null <<'EOF'
[connection]
wifi.powersave = 2
EOF
python3 - "$APP_DIR/settings.json" "$SSID" "$PASS" <<'PY'
import json, sys
path, ssid, pw = sys.argv[1:4]
try:
    s = json.load(open(path))
except Exception:
    s = {}
s.update({"wifi_ssid": ssid, "wifi_pass": pw})
json.dump(s, open(path, "w"), indent=2)
PY

say "Setting up the kiosk session…"
mkdir -p "$HOME/.config/labwc"
RC="$HOME/.config/labwc/rc.xml"
[ -f "$RC" ] || cp /etc/xdg/labwc/rc.xml "$RC" 2>/dev/null || true
if grep -q 'mouseEmulation="yes"' "$RC" 2>/dev/null; then
  sed -i 's/mouseEmulation="yes"/mouseEmulation="no"/' "$RC"
elif ! grep -q 'mouseEmulation="no"' "$RC" 2>/dev/null; then
  cat > "$RC" <<'XML'
<?xml version="1.0"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <touch deviceName="Goodix Capacitive TouchScreen" mapToOutput="DSI-1" mouseEmulation="no"/>
</openbox_config>
XML
fi
ENVF="$HOME/.config/labwc/environment"
grep -q '^XCURSOR_SIZE=' "$ENVF" 2>/dev/null || echo 'XCURSOR_SIZE=1' >> "$ENVF"
cat > "$HOME/.config/labwc/autostart" <<EOF
wlr-randr --output DSI-1 --transform 270
/usr/bin/swaybg -c '#000000' &
$APP_DIR/kiosk.sh &
EOF
chmod +x "$HOME/.config/labwc/autostart"
if [ ! -e /etc/xdg/labwc/autostart.distrib ]; then
  sudo dpkg-divert --local --rename --add /etc/xdg/labwc/autostart >/dev/null
fi
sudo tee /etc/xdg/labwc/autostart </dev/null >/dev/null
sudo raspi-config nonint do_boot_behaviour B4 || true

say "Granting the dash the actions it needs…"
NMCLI="$(command -v nmcli || echo /usr/bin/nmcli)"
sudo tee /etc/sudoers.d/spooled >/dev/null <<EOF
$USER_NAME ALL=(ALL) NOPASSWD: /sbin/reboot, /sbin/shutdown, /usr/bin/systemctl reboot, /usr/sbin/reboot, /usr/sbin/shutdown, /usr/bin/systemctl restart spooled-server, $NMCLI, /usr/bin/date, /bin/date, /usr/sbin/fake-hwclock, /usr/bin/timedatectl
EOF
sudo chmod 440 /etc/sudoers.d/spooled
sudo visudo -c -f /etc/sudoers.d/spooled >/dev/null

say "Trimming boot time…"
sudo systemctl disable NetworkManager-wait-online.service e2scrub_reap.service \
  cups.service cups.socket cups.path bluetooth.service rpcbind.service \
  rpcbind.socket nfs-blkmap.service nfs-client.target udisks2.service \
  rpi-eeprom-update.service apt-daily.timer apt-daily-upgrade.timer \
  man-db.timer rp1-test.service glamor-test.service >/dev/null 2>&1 || true
[ -d /etc/cloud ] && sudo touch /etc/cloud/cloud-init.disabled 2>/dev/null || true
mkdir -p "$HOME/.config/systemd/user"
for u in pipewire.socket pipewire-pulse.socket pipewire.service \
         pipewire-pulse.service wireplumber.service filter-chain.service \
         mpris-proxy.service; do
  ln -sf /dev/null "$HOME/.config/systemd/user/$u"
done
sudo tee /usr/local/sbin/spooled-checkpoint >/dev/null <<'PY'
#!/usr/bin/env python3
import fcntl, os, struct, sys, time
EXT4_IOC_CHECKPOINT = 0x4004662b
def checkpoint():
    fd = os.open("/", os.O_RDONLY)
    try:
        fcntl.ioctl(fd, EXT4_IOC_CHECKPOINT, struct.pack("I", 0))
    finally:
        os.close(fd)
try:
    checkpoint()
except OSError as e:
    print("checkpoint not supported here: %s" % e, flush=True)
    sys.exit(0)
while True:
    time.sleep(15)
    try:
        checkpoint()
    except OSError as e:
        print("checkpoint failed: %s" % e, flush=True)
        time.sleep(300)
PY
sudo chmod 755 /usr/local/sbin/spooled-checkpoint
sudo tee /etc/systemd/system/spooled-checkpoint.service >/dev/null <<'UNIT'
[Unit]
Description=SpooledOS: keep the root journal empty so a power cut boots fast
After=local-fs.target

[Service]
ExecStart=/usr/local/sbin/spooled-checkpoint
Restart=on-failure
Nice=10

[Install]
WantedBy=multi-user.target
UNIT
sudo sed -i 's/^auto_initramfs=1/auto_initramfs=0/' "$BOOTCFG"
sudo sync

if [ -f "$APP_DIR/boot-splash.sh" ]; then
  say "Installing the boot splash…"
  sudo bash "$APP_DIR/boot-splash.sh" >/dev/null 2>&1 || echo "    (boot splash skipped)"
fi
say "Starting SpooledOS…"
sudo systemctl daemon-reload
sudo systemctl enable spooled-server spooled-checkpoint >/dev/null 2>&1
sudo systemctl restart spooled-server
sudo systemctl restart spooled-checkpoint 2>/dev/null || true

cat <<EOF

============================================================
 SpooledOS Community is installed.

 Reboot to finish:   sudo reboot

 After the reboot the screen goes straight into the dash, and
 the Pi makes a WiFi network called "$SSID" for your DS1.
 If you installed over SSH, that is when you lose the
 connection - the Pi is busy being the DS1's WiFi now.

 Next: join your DS1 to "$SSID" (password: $PASS).
 To get updates later, join the dash to your home WiFi from
 Settings -> Network, then Settings -> Check for updates.
============================================================
EOF
