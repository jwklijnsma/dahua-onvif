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

## Podman + systemd (Quadlet)

[`podman/dahua-onvif.container`](podman/dahua-onvif.container) is a Quadlet unit: systemd runs the container directly, restarts it on failure, starts it at boot, and `podman auto-update` keeps the image current. Needs Podman 4.4 or newer.

**Rootful** (system service):

```bash
sudo mkdir -p /etc/dahua-onvif /etc/containers/systemd
sudo cp config.yaml /etc/dahua-onvif/config.yaml          # edit dahua.host / onvif.name
sudo cp podman/dahua-onvif.container /etc/containers/systemd/
printf '%s' 'VTO-PASSWORD' | sudo podman secret create dahua-password -
sudo systemctl daemon-reload
sudo systemctl start dahua-onvif
sudo systemctl enable --now podman-auto-update.timer      # optional: daily image updates
```

**Rootless** (user service):

```bash
mkdir -p ~/.config/dahua-onvif ~/.config/containers/systemd
cp config.yaml ~/.config/dahua-onvif/config.yaml
cp podman/dahua-onvif.container ~/.config/containers/systemd/
printf '%s' 'VTO-PASSWORD' | podman secret create dahua-password -
systemctl --user daemon-reload
systemctl --user start dahua-onvif
loginctl enable-linger "$USER"                            # start at boot without login
systemctl --user enable --now podman-auto-update.timer    # optional
```

Quadlet units are generated, so `systemctl enable` is not used; the `[Install]` section takes care of boot start. Logs: `journalctl -u dahua-onvif -f` (add `--user` for rootless).

The container runs with host networking (needed for WS-Discovery), a read-only root filesystem, `no-new-privileges` and only `NET_BIND_SERVICE` (to listen on port 80). Make sure nothing else on the host uses port 80, or set another `onvif.port`. Rootless podman cannot bind ports below 1024 by default: use `onvif.port: 8000` there, or allow it with `sysctl net.ipv4.ip_unprivileged_port_start=80`. On a firewalld host, open the ports:

```bash
sudo firewall-cmd --permanent --add-port=80/tcp --add-port=3702/udp && sudo firewall-cmd --reload
```

For an ONVIF login, create an `onvif-password` secret, set `onvif.username` in the config and uncomment the second `Secret=` line.

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
