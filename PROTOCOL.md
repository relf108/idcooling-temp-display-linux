# ID-COOLING Temp Display — HID protocol

Reverse-engineered from the official **ID-COOLING Temp Display** Windows app
(used by the FX TD / FROZN TD series) and verified on real hardware.

## Device

| | |
|---|---|
| USB ID | `1a86:e317` (QinHeng/WCH) |
| USB product string | `IDCOOL-C` |
| Interface | single HID interface, `/dev/hidrawN` |
| Report descriptor | vendor usage page `0xFF00`; 64-byte IN and OUT reports, **no** report ID |

The device is **write-only / fixed-function**: the host sends a numeric value
and a command, and the device *firmware* renders the number. It is **not** a
framebuffer — there is no command to upload pixels, bitmaps, or images.

## Command report (host → device)

Every command is a 64-byte HID output report:

| Offset | Value | Meaning |
|-------:|-------|---------|
| 0 | `0x55` | header |
| 1 | `0xBB` | header |
| 2 | `0x02` | payload length (always 2) |
| 3 | `cmd`  | command id (see below) |
| 4 | `value >> 8 & 0xFF` | value, 16-bit **big-endian** high byte |
| 5 | `value & 0xFF` | value low byte |
| 6 | `checksum` | `(0x55 + 0xBB + 0x02 + cmd + hi + lo) & 0xFF` |
| 7..63 | `0x00` | padding to 64 bytes |

### Commands

| `cmd` | Name | `value` |
|------:|------|---------|
| 1 | `CPU_TEMPERATURE` | integer °C |
| 2 | `CPU_FREQUENCY`   | MHz (vendor app shows it as GHz) |
| 3 | `CPU_USAGE`       | percent |
| 4 | `SHOW`            | `1` = screen on, `0` = off |

Send `SHOW(1)` once to enable the display, then push the metric you want on an
interval (the vendor app uses ~1 s).

The wire field can encode any unsigned 16-bit value, but that does not mean all
values are meaningful to the firmware. The Linux driver therefore applies a
separate host-side safety policy: temperature `0..150` °C, frequency
`0..20000` MHz, usage `0..100`, and show `0..1`. These limits are sanity checks,
not additional claims about the protocol. It also limits updates to at most
five per second; the default remains the vendor-like one-second interval.

### Writing on Linux

The device uses **unnumbered** HID reports, so a raw `write()` to `/dev/hidrawN`
must be prefixed with a `0x00` report-id byte (the kernel strips it):

```python
os.write(fd, b"\x00" + report)   # 65 bytes total: 0x00 + 64-byte report
```

### Worked example

CPU temperature = 77 °C, `cmd = 1`, `value = 77 = 0x004D`:

```
checksum = (0x55 + 0xBB + 0x02 + 0x01 + 0x00 + 0x4D) & 0xFF = 0x60
report   = 55 BB 02 01 00 4D 60 00 00 ... 00   (64 bytes)
```

## How it was found

The vendor app is an Electron application. Its logic ships as readable
JavaScript inside `resources/app.asar`, which can be extracted on Linux with
**no Windows and no Wine**:

```bash
# 1. fetch the official installer (NSIS self-extracting exe)
curl -fLO 'https://static.idcooling.com/link/20251009/ID-COOLING%20Temp%20Display%20Installer_5w8x73.exe'

# 2. crack the installer with 7-Zip -> $PLUGINSDIR/app-64.7z
7z x 'ID-COOLING Temp Display Installer_5w8x73.exe' -oinstaller

# 3. crack the embedded app archive -> resources/app.asar
7z x installer/'$PLUGINSDIR'/app-64.7z resources/app.asar -oapp

# 4. unpack the asar -> dist-electron/main/index.js + dist/assets/index-*.js
npx @electron/asar extract app/resources/app.asar src
```

The encoder lives in the renderer bundle (`dist/assets/index-*.js`) as the
function the app calls `IL(cmd, value)`, and the device write is the
`device-send-frame` IPC handler in `dist-electron/main/index.js`
(`new HID(path); hid.write(...)` via node-hid). The full command set is the
`Yd` enum above — there is no image/upload command anywhere, which is how we
know the screen can only show firmware-rendered numbers.
