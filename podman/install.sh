#!/bin/sh
# Install dahua-onvif (+ go2rtc live view) as Podman Quadlet systemd services.
#
#   curl -fsSL https://raw.githubusercontent.com/jwklijnsma/dahua-onvif/main/podman/install.sh \
#     | sh -s -- --vto 10.0.2.45
#
# Run as root. Re-running is safe: existing configs are kept unless --force.
set -eu

REPO_RAW="https://raw.githubusercontent.com/jwklijnsma/dahua-onvif/main"
VTO=""
VTO_PORT=80
VTO_USER="admin"
PASSWORD="${DAHUA_PASSWORD:-}"
NAME=Voordeur
PORT=80
IMAGE=""
GO2RTC=1
FORCE=0

usage() {
  cat <<EOF
Usage: install.sh --vto IP [options]

  --vto IP           VTO IP address (required for a new install)
  --vto-port N       VTO HTTP port (default 80)
  --user NAME        VTO username (default admin)
  --password PW      VTO password (default: \$DAHUA_PASSWORD, else asked)
  --name NAME        camera name shown in the NVR (default Voordeur)
  --port N           ONVIF / status page port on this host (default 80)
  --no-go2rtc        skip the go2rtc live-view service
  --image REF        override the dahua-onvif image
  --force            overwrite existing config files
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --vto) VTO="$2"; shift 2 ;;
    --vto-port) VTO_PORT="$2"; shift 2 ;;
    --user) VTO_USER="$2"; shift 2 ;;
    --password) PASSWORD="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-go2rtc) GO2RTC=0; shift ;;
    --image) IMAGE="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
command -v podman >/dev/null || { echo "podman is not installed" >&2; exit 1; }
[ -x /usr/libexec/podman/quadlet ] || [ -x /usr/lib/podman/quadlet ] || \
  echo "warning: Quadlet not found - Podman 4.4 or newer is required" >&2

CONF=/etc/dahua-onvif
UNITS=/etc/containers/systemd
mkdir -p "$CONF" "$UNITS"

# Unit files: use the ones next to this script when present, else download.
SRC="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo /nonexistent)"
fetch() {
  if [ -f "$SRC/$1" ]; then cp "$SRC/$1" "$2"
  else curl -fsSL "$REPO_RAW/podman/$1" -o "$2"; fi
}

write_config() {  # file, content-generator
  if [ -f "$1" ] && [ "$FORCE" -eq 0 ]; then
    echo "keeping existing $1 (use --force to overwrite)"
    return 0
  fi
  [ -n "$VTO" ] || { echo "--vto IP is required to create $1" >&2; exit 2; }
  "$2" > "$1"
  chmod 644 "$1"
  echo "wrote $1"
}

gen_config() {
  cat <<EOF
dahua:
  host: $VTO
  port: $VTO_PORT
  rtsp_port: 554
  username: $VTO_USER
  channel: 1
  events: true

onvif:
  port: $PORT
  name: $NAME
  discovery: true
  # username / password: set to require an ONVIF login from the NVR

stream:
  embed_credentials: true
  # Let the NVR use go2rtc's single VTO connection instead of the VTO directly:
  # main_url: rtsp://<this-host-ip>:8554/voordeur
  # sub_url:  rtsp://<this-host-ip>:8554/voordeur_sub

live:
  go2rtc_url: auto
  stream: voordeur
  mode: webrtc,mse

doorbell:
  hold_seconds: 3
  extra_codes: []

log_level: INFO
EOF
}

gen_go2rtc() {
  cat <<EOF
streams:
  voordeur:
    - rtsp://$VTO_USER:\${DAHUA_PASSWORD}@$VTO:554/cam/realmonitor?channel=1&subtype=0
  voordeur_sub:
    - rtsp://$VTO_USER:\${DAHUA_PASSWORD}@$VTO:554/cam/realmonitor?channel=1&subtype=1

api:
  listen: ":1984"
rtsp:
  listen: ":8554"
webrtc:
  listen: ":8555"
EOF
}

write_config "$CONF/config.yaml" gen_config
[ "$GO2RTC" -eq 1 ] && write_config "$CONF/go2rtc.yaml" gen_go2rtc

# VTO password -> podman secrets:
#   dahua-password      plain, for dahua-onvif
#   dahua-password-url  percent-encoded, for go2rtc's rtsp://user:pass@host URL
urlencode() { printf '%s' "$1" | od -An -tx1 -v | tr -d ' \n' | sed 's/../%&/g'; }
set_secret() {  # name, value
  podman secret exists "$1" 2>/dev/null && podman secret rm "$1" >/dev/null
  printf '%s' "$2" | podman secret create "$1" - >/dev/null
}
ask_password() {
  [ -r /dev/tty ] || { echo "no VTO password: pass --password or set DAHUA_PASSWORD" >&2; exit 2; }
  printf 'VTO password for %s: ' "$VTO_USER" > /dev/tty
  stty -echo < /dev/tty 2>/dev/null || true
  read -r PASSWORD < /dev/tty
  stty echo < /dev/tty 2>/dev/null || true
  echo > /dev/tty
}

if [ -z "$PASSWORD" ]; then
  if podman secret exists dahua-password 2>/dev/null; then
    # reuse the stored password (needs podman 4.8+ for --showsecret)
    PASSWORD="$(podman secret inspect --showsecret --format '{{.SecretData}}' dahua-password 2>/dev/null || true)"
  fi
  [ -n "$PASSWORD" ] || ask_password
fi
set_secret dahua-password "$PASSWORD"
set_secret dahua-password-url "$(urlencode "$PASSWORD")"
echo "stored VTO password in podman secrets dahua-password and dahua-password-url"

fetch dahua-onvif.container "$UNITS/dahua-onvif.container"
if [ -n "$IMAGE" ]; then
  sed -i "s|^Image=.*|Image=$IMAGE|" "$UNITS/dahua-onvif.container"
fi
SERVICES=dahua-onvif
if [ "$GO2RTC" -eq 1 ]; then
  fetch go2rtc.container "$UNITS/go2rtc.container"
  SERVICES="$SERVICES go2rtc"
fi

systemctl daemon-reload
for s in $SERVICES; do
  systemctl restart "$s"
done
sleep 3

ok=1
for s in $SERVICES; do
  if systemctl is-active --quiet "$s"; then echo "$s: running"
  else echo "$s: NOT running - journalctl -u $s -n 50" >&2; ok=0; fi
done

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
P=""; [ "$PORT" = 80 ] || P=":$PORT"
cat <<EOF

Status page / live view:  http://${IP:-<this-host>}$P/
Add to your NVR (ONVIF):  host ${IP:-<this-host>}, port $PORT
Logs:                     journalctl -u dahua-onvif -f
Firewall (if enabled):    tcp $PORT, udp 3702$( [ "$GO2RTC" -eq 1 ] && echo ", tcp 1984, tcp+udp 8555, tcp 8554")
EOF
[ "$ok" -eq 1 ]
