#!/usr/bin/env bash
set -u
IFACE="${IFACE:-wlan0}"
AP_CON="${AP_CON:-spooled-ap}"
CLI_CON="${CLI_CON:-spooled-client}"

case "${1:-status}" in
  status)
    active=$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
             | awk -F: -v i="$IFACE" '$2==i{print $1; exit}')
    ip=$(nmcli -t -f IP4.ADDRESS device show "$IFACE" 2>/dev/null \
         | head -1 | cut -d: -f2 | cut -d/ -f1)
    if [ "$active" = "$AP_CON" ]; then role=ap
    elif [ -n "$active" ];        then role=client
    else                               role=down
    fi
    if ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; then net=yes; else net=no; fi
    saved=$(nmcli -g 802-11-wireless.ssid connection show "$CLI_CON" 2>/dev/null | head -1)
    echo "role=$role"
    echo "ssid=${active:-}"
    echo "ip=${ip:-}"
    echo "internet=$net"
    echo "saved=${saved:-}"
    ;;
  scan)
    nmcli -t -f SSID,SIGNAL device wifi list ifname "$IFACE" --rescan yes 2>/dev/null \
      | awk -F: 'length($1){print $1"\t"$2}' | sort -t$'\t' -k2 -nr | awk '!seen[$0]++' | head -25
    ;;
  client)
    ssid="${2:?ssid required}"; pass="${3:-}"
    sudo -n nmcli connection down "$AP_CON" >/dev/null 2>&1 || true

    sudo -n nmcli connection delete "$CLI_CON" >/dev/null 2>&1 || true
    sudo -n nmcli connection add type wifi ifname "$IFACE" con-name "$CLI_CON" \
      autoconnect no ssid "$ssid" 802-11-wireless.powersave 2 || exit 1
    if [ -n "$pass" ]; then
      sudo -n nmcli connection modify "$CLI_CON" \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pass" || exit 1
    fi
    sudo -n nmcli connection up "$CLI_CON"
    ;;
  reconnect)
    nmcli -g 802-11-wireless.ssid connection show "$CLI_CON" >/dev/null 2>&1 \
      || { echo "no saved network" >&2; exit 1; }
    sudo -n nmcli connection down "$AP_CON" >/dev/null 2>&1 || true
    sudo -n nmcli connection up "$CLI_CON"
    ;;
  ap)
    sudo -n nmcli connection up "$AP_CON"
    ;;
  apcfg)
    ssid="${2:?ssid required}"; pass="${3:?pass required}"
    sudo -n nmcli connection modify "$AP_CON" 802-11-wireless.ssid "$ssid" wifi-sec.psk "$pass" || exit 1
    sudo -n nmcli connection up "$AP_CON" || true
    ;;
  *)
    echo "usage: netmode.sh {status|scan|client SSID PASS|reconnect|ap|apcfg SSID PASS}" >&2; exit 2 ;;
esac
