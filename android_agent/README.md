# Android Agent

Local ADB bridge for an Android phone. Runs on `127.0.0.1:8767`.
Telegram Personal uses **8768** so this service keeps port 8767.

Copy `config.example.json` to `config.json` and fill in the ADB/Tailscale paths
and phone addresses before the first run.

## Endpoints

All endpoints except `/health` require `X-API-Key: <api_key>`. The service
refuses authenticated requests with HTTP 503 when `api_key` is empty, so set a
long random value in `config.json` before starting it. Never commit that file.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Service, ADB devices, Tailscale peers, stop flag |
| `POST /device/reconnect` | Force Tailscale → LAN → USB connection sequence |
| `POST /device/pair` | `adb pair <host>:<pair_port> <code>` |
| `POST /device/info` | Model, Android version, battery, storage, Wi-Fi |
| `POST /device/alarms/list` | Next pending Clock alarm from `dumpsys alarm` (filters out system timers) |
| `POST /device/alarm/create` | `SET_ALARM` intent pinned to the Samsung Clock package (hour, minute, label, skip_ui) |
| `POST /device/alarms/show` | Open the phone's alarm screen |
| `POST /device/notifications` | Active notifications |
| `POST /device/volume` | Get/set media volume |
| `POST /device/keyevent` | Power, wake, media keys |
| `POST /device/open_url` | Open a URL on the phone |
| `POST /device/apps` | Installed user packages |
| `POST /device/battery` | Battery details |
| `POST /device/storage` | `/data` usage |
| `POST /device/wifi` | Wi-Fi IP and SSID lines |
| `POST /device/log` | Connection log tail |
| `POST /emergency_stop` | Set/clear stop flag (`{"active": true}`) |
| `POST /stop/clear` | Clear the stop flag |

## Connection sequence

1. `adb devices` (USB already connected).
2. `tailscale_phone_ip` from `config.json`.
3. Auto-discover Android peers from `tailscale status --json` (host contains phone/galaxy/samsung, or OS=android).
4. `local_phone_ip` from `config.json`.
5. Retry every 60 seconds while disconnected; keepalive while connected.

## Phone setup

1. Enable **Developer options → USB debugging**.
2. For wireless ADB: connect USB once, run `adb tcpip 5555`, then unplug.
3. For Android 11+ wireless debugging: use `POST /device/pair` with the pairing port/code, then connect to the separate wireless-debugging port.
4. If the phone is on the same Tailscale tailnet, set its IP in `config.json` or rely on auto-discovery.
5. `clock_package` in `config.json` pins alarm creation to Samsung Clock (`com.sec.android.app.clockpackage`) so the Android chooser does not appear. Change it only if the default clock app changes.
6. The wireless-debugging connect port is random and changes when the feature is restarted. Update `adb_port` or send a new `/phone_connect` when it changes.

## Run

```powershell
..\mcp_server\.venv\Scripts\python.exe .\main.py
```

Service install: `install_service.ps1` (NSSM, Automatic). Run it from an
elevated shell.
