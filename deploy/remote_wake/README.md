# OTHVM — Remote wake from Telegram (free)

When the laptop sleeps, the main OTHVM bot sleeps with it. This component runs a
small Telegram bot on an always-on Android phone (Termux). Send `/wake` and the
phone sends a Wake-on-LAN magic packet to the laptop on the same Wi-Fi network.

```
Telegram → Termux bot on phone → UDP magic packet → laptop Wi-Fi card → laptop wakes
```

## 1. Laptop prerequisites

- Wi-Fi adapter: **Wake on Magic Packet = Enabled**.
- Confirm Windows has armed the adapter:

  ```powershell
  powercfg /devicequery wake_armed
  powercfg /a
  Get-NetAdapterAdvancedProperty -Name 'Wi-Fi' |
    Where-Object DisplayName -match 'Wake|Magic'
  ```

- The adapter must wake from **S3** (the only sleep state available on most
  laptops).
- Waking from a full shutdown is not guaranteed; wake from Sleep is.

## 2. Create a second Telegram bot

The main OTHVM bot uses a webhook, so it cannot share a token with this poller.
Create a separate bot:

1. Open `@BotFather` in Telegram.
2. `/newbot` → choose a name → choose a username ending in `bot`.
3. Copy the new token (`123456:ABC...`).

## 3. Install Termux on the phone

Install **Termux** from F-Droid (not the outdated Play Store build):

- <https://f-droid.org/packages/com.termux/>

Optionally install **Termux:Boot** so the bot starts after a phone reboot:

- <https://f-droid.org/packages/com.termux.boot/>

Then open Termux and run:

```bash
pkg update -y && pkg upgrade -y
pkg install -y python
termux-wake-lock
```

Disable battery optimization for Termux:
`Settings → Apps → Termux → Battery → Unrestricted`.

## 4. Copy the script and configure it

Copy `termux_wake_bot.py` to the phone (Tailscale, ADB, or any file-transfer
method). Create the environment file `~/.othvm_wake.env`:

```bash
cat > ~/.othvm_wake.env <<'EOF'
WAKE_BOT_TOKEN=your-new-bot-token
OWNER_CHAT_ID=your-telegram-chat-id
LAPTOP_MAC=AA:BB:CC:DD:EE:FF
BROADCAST_IP=192.168.1.255
WOL_PORT=9
LAPTOP_IP=192.168.1.10
WOL_REPEAT=3
EOF
chmod 600 ~/.othvm_wake.env
```

- `LAPTOP_MAC` is the Wi-Fi adapter's MAC address (`getmac /v` on Windows).
- `BROADCAST_IP` is the subnet broadcast address of the laptop's network.
- `LAPTOP_IP` is optional; it is used for the `/status` ping.
- Update these values if the router assigns a different address.

## 5. Run

```bash
python ~/termux_wake_bot.py
```

Keep the session open and test from Telegram:

- `/start`
- `/wake`
- `/status`

The first wake can take 10–20 seconds.

### Run in the background

```bash
nohup python ~/termux_wake_bot.py >> ~/wake-bot.log 2>&1 &
```

### Start automatically after a phone reboot

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-wake-bot.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
nohup python ~/termux_wake_bot.py >> ~/wake-bot.log 2>&1 &
EOF
chmod +x ~/.termux/boot/start-wake-bot.sh
```

Open the **Termux:Boot** app once after installing it.

## 6. Troubleshooting

- **Phone on mobile data:** the magic packet cannot reach the LAN. The phone and
  laptop must be on the same Wi-Fi network.
- **Router client isolation / AP isolation:** disable it.
- **Laptop in deep sleep or shut down:** try waking from Sleep first. Waking from
  shutdown requires BIOS support.
- **Laptop does not wake over Wi-Fi:** some adapters only wake over Ethernet. If
  Ethernet is connected, use that network's `BROADCAST_IP`.
- **Windows Fast Startup:** disable it.
- **Bot does not respond:** check `cat ~/wake-bot.log`.

## Alternative: keep the laptop awake

If you prefer not to rely on WoL, keep the laptop awake while plugged in:

```powershell
# Closing the lid does nothing (AC)
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
# No sleep on AC
powercfg /change standby-timeout-ac 0
powercfg /setactive SCHEME_CURRENT
```

To revert:

```powershell
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 1
powercfg /change standby-timeout-ac 30
powercfg /setactive SCHEME_CURRENT
```
