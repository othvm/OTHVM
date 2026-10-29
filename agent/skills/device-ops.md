# Device Ops

Use for Windows or Android operations: diagnostics, screenshots, apps, windows, files, network, power, security, clipboard, taps, swipes, alarms, notifications.

## Check first
- `computer` `system` `catalog` lists every Windows action; read it before guessing a name.
- `computer` `system` `info` for host, user, CPU/RAM/disk, permission mode.
- `phone` status: `connected=true` only when the tool says so. If false, report the real cause (Tailscale, USB debugging, ADB 503) and offer reconnect steps.

## Windows categories
`system, files, apps, windows, input, browser, network, display, audio, devices, security, power, startup, registry`

- Diagnose: `system` battery/uptime/disk/memory/cpu/gpu/event_log/updates/wsl/docker, `security` defender/bitlocker/secure_boot/tpm, `devices` bluetooth/usb/printers/drivers/problem_devices, `network` interfaces/ip/wifi/ping/dns/ports/firewall, `power` plans/sleep_states/last_wake.
- Files: `files` read/write/append/list/search/move/copy, hash, zip/unzip, permissions, owner, content search, duplicates, large files, shares. Deletes go to trash; undo with `computer` `files` `undo`.
- Act: `apps` open/close/focus/uninstall/defaults, `windows` list/focus/snap/resize, `input` keyboard/mouse/media keys, `audio` volume/devices/playback, `display` resolution/wallpaper/theme, `browser` navigate/click/type/extract.
- Guarded: `registry` write/delete and `startup` add/remove are T2; sensitive hives and credential keys are blocked.
- Use `vision` for OCR or understanding a screenshot.

## Android
- Safe reads: status, info, battery, storage, wifi, notifications, apps, alarms.
- Writes: open_url, alarm_create, volume, keyevent, shell taps/swipes/text.
- Calls, SMS, and installs are T2; factory reset, deleting data, or disabling security are T3.
- One `shell` command at a time; quote paths; prefer documented actions over raw shell.

## Approvals
- T1: screenshot, open app, clipboard read, list files, read notifications, diagnostics.
- T2: install/uninstall, settings, registry write, startup change, send SMS/call, external file upload, kill a normal app.
- T3: delete files, kill a critical process, shutdown/restart, factory reset, credential changes.

## Reporting
State the device, the action, and the observable result. If a command times out, say so and give the last real output. Never claim a tap or alarm succeeded without tool evidence.
