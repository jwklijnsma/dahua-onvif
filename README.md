# dahua-onvif

Turns a Dahua VTO doorbell (tested target: VTO2111D-P) into an ONVIF camera for an NVR or VMS.

It reads the device the same way the [rroller/dahua](https://github.com/rroller/dahua) Home Assistant integration does:

| Input (VTO) | Used for |
|---|---|
| `:80` `/cgi-bin/magicBox.cgi` (digest auth) | model, serial, firmware |
| `:80` `/cgi-bin/configManager.cgi?name=Encode` | real main/sub resolution, fps, bitrate |
| `:80` `/cgi-bin/snapshot.cgi` | ONVIF snapshot (auth handled here, the NVR needs none) |
| `:80` `/cgi-bin/eventManager.cgi?action=attach` | motion + doorbell button events |
| `:554` `/cam/realmonitor?channel=1&subtype=0/1` | video (main + sub profile) |

| Output (ONVIF) | |
|---|---|
| `http://<host>/onvif/device_service` | Device + Media (Profile S), 2 profiles |
| PullPoint events | `tns1:VideoSource/MotionAlarm`, `tns1:RuleEngine/CellMotionDetector/Motion`, `tns1:Device/Trigger/DigitalInput` (InputToken `DoorBell`) |
| WS-Discovery `udp/3702` | auto-discovery by the NVR |
| `http://<host>/` | status page: live snapshot, motion/doorbell state, stream info |
| `http://<host>/healthz` | status + current event state (JSON) |

## Run

The image is published to GHCR for `amd64`, `arm64` and `arm/v7` (Raspberry Pi):

```bash
docker pull ghcr.io/jwklijnsma/dahua-onvif:latest
```

With compose (edit `dahua.host` and `onvif.name` in `config.yaml` first):

```bash
export DAHUA_PASSWORD='...'
docker compose up -d
docker logs -f dahua-onvif
```

Plain docker:

```bash
docker run -d --name dahua-onvif --network host --restart unless-stopped \
  -v "$PWD/config.yaml:/config/config.yaml:ro" \
  -e DAHUA_PASSWORD='...' \
  ghcr.io/jwklijnsma/dahua-onvif:latest
```

Without Docker: `pip install -r requirements.txt && python dahua_onvif.py config.yaml`

## Podman + systemd (Quadlet) - quick install

One command on the podman host (as root) sets up both services: the ONVIF bridge and the [go2rtc](https://github.com/AlexxIT/go2rtc) live view.

```bash
curl -fsSL https://raw.githubusercontent.com/jwklijnsma/dahua-onvif/main/podman/install.sh \
  | sudo sh -s -- --vto 10.0.2.45 --name Voordeur
```

It asks for the VTO password (or pass `--password` / set `DAHUA_PASSWORD`), then:

- writes `/etc/dahua-onvif/config.yaml` and `/etc/dahua-onvif/go2rtc.yaml` (kept on re-run unless `--force`)
- stores the password in the podman secrets `dahua-password` (bridge) and `dahua-password-url` (percent-encoded, for go2rtc's RTSP URL)
- installs `dahua-onvif.container` and `go2rtc.container` in `/etc/containers/systemd/`, reloads systemd and starts them

Open `http://<host>/` for the status page with **live video**, and add `<host>` port `80` as an ONVIF camera in your NVR. Re-run the installer any time to update the unit files or change the password. Other options: `--port`, `--user`, `--vto-port`, `--no-go2rtc`; see `install.sh --help`. Needs Podman 4.4 or newer.

Daily image updates: `systemctl enable --now podman-auto-update.timer`. Logs: `journalctl -u dahua-onvif -f` / `journalctl -u go2rtc -f`. Quadlet units are generated, so there is no `systemctl enable`; the `[Install]` section handles start at boot.

### Live view (go2rtc)

go2rtc takes the VTO's RTSP stream and passes it to the browser as WebRTC (MSE as fallback) without re-encoding, so latency stays under a second and CPU use is near zero. The status page uses it automatically when go2rtc answers on `http://<host>:1984` (`live.go2rtc_url: auto`) and falls back to a refreshing snapshot otherwise. go2rtc's own UI on port 1984 has no login, so keep it on your LAN.

The VTO only accepts a few RTSP clients at a time. To have the NVR use go2rtc's single connection instead, set `stream.main_url` / `sub_url` in `config.yaml` to `rtsp://<host>:8554/voordeur` / `voordeur_sub`.

### Security and ports

Both containers use host networking (needed for WS-Discovery multicast and WebRTC). The bridge runs with a read-only root filesystem, `no-new-privileges` and every capability dropped except `NET_BIND_SERVICE`, so it can listen on port 80. It runs as container root so that capability works on every host, including LXC.

Ports: 80/tcp (ONVIF + status page), 3702/udp (discovery), 1984/tcp (go2rtc web), 8554/tcp (RTSP restream), 8555/tcp+udp (WebRTC). With firewalld:

```bash
sudo firewall-cmd --permanent --add-port=80/tcp --add-port=3702/udp --add-port=1984/tcp --add-port=8554/tcp --add-port=8555/tcp --add-port=8555/udp && sudo firewall-cmd --reload
```

For an ONVIF login, create an `onvif-password` podman secret, set `onvif.username` in the config and uncomment the second `Secret=` line in the unit.

### Manual / rootless install

Copy `podman/*.container` to `~/.config/containers/systemd/`, create `~/.config/dahua-onvif/config.yaml` and `go2rtc.yaml` from the examples in this repo, create the secrets `dahua-password` (plain) and `dahua-password-url` (percent-encoded, e.g. `@` → `%40`) with `podman secret create`, then `systemctl --user daemon-reload && systemctl --user start dahua-onvif go2rtc` and `loginctl enable-linger "$USER"`. Rootless podman cannot bind ports below 1024 by default: set `onvif.port: 8000`, or allow it with `sysctl net.ipv4.ip_unprivileged_port_start=80`.

## Image tags

| Tag | When |
|---|---|
| `latest` | every push to `main` |
| `1.2.3`, `1.2` | git tag `v1.2.3` |
| `sha-abc1234` | every build |

CI runs the end-to-end test (`test/`) against a mock VTO before building.

## Test locally

```bash
pip install -r requirements.txt onvif-zeep
cd test
python mock_dahua.py & python ../dahua_onvif.py test_config.yaml & sleep 3
python test_client.py
```

## Notes

- **Doorbell press** is detected from `BackKeyLight` with State 1/2 (same as the HA integration), `Invite`, `DoorBell` or `CallSnap`. Firmware differs: set `log_level: DEBUG`, press the button, and add any other code you see to `doorbell.extra_codes`.
- If the event stream never connects on port 80, your firmware only sends VTO events over the proprietary port 5000 protocol (which the HA integration also uses for some VTOs). Video and snapshots still work.
- One virtual camera per IP: this service runs on the host's own IP, so no macvlan is needed. To run several, give each its own `onvif.port` (discovery still works because each has its own UUID).
- ONVIF media1 can only advertise H.264; keep the VTO streams on H.264.
- Set `onvif.username`/`password` to require login from the NVR; WS-UsernameToken (digest and plain text) is supported.
