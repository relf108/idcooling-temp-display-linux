# idcooling-temp-display-linux

Linux driver for **ID-COOLING "Temp Display"** coolers — the little fan/cooler
screens that show your CPU temperature. There's no official Linux software, so
this is a tiny, dependency-free daemon that speaks the vendor's USB-HID
protocol (reverse-engineered and verified on real hardware).

If your cooler's display enumerates as **USB `1a86:e317`** (product string
`IDCOOL-C`), this is for you. Known to cover the ID-COOLING **FX TD** /
**FROZN TD** "Temp Display" series, and rebrands such as the cooler shipped in
some **Aftershock** PCs.

> **Scope / limitation:** this screen is a *fixed-function* display. The
> firmware renders the numbers; the host can only send **CPU temperature,
> frequency, usage, and screen on/off**. It is **not** a framebuffer — you
> cannot draw arbitrary images on it. (Verified: the vendor app has no
> image/upload command.) See [`PROTOCOL.md`](PROTOCOL.md).

## Installation

First confirm you actually have this device:

```bash
lsusb | grep -i 1a86:e317
```

### Requirements

- Python 3 (standard library only)
- Linux `hidraw` device

CPU temperature is read from `/sys/class/hwmon` (AMD `k10temp` / `zenpower`
and Intel `coretemp` auto-detected). On systems with an unrecognized sensor,
select it explicitly with `--temp-path`.

### Try it first (no install)

In some cases the cooler display may take a second or two to reflect a value.
If so, `--once` may be insufficient; use the continuous command and stop it
with `Ctrl+C` after confirming the display updates.

```bash
sudo ./idcool_display.py --once            # one update, then exit
sudo ./idcool_display.py                   # run continuously (temp, every 1s)
sudo ./idcool_display.py --metric usage    # or: usage / freq
```

`sudo` is normally needed for a one-off test because `/dev/hidraw*` is
root-only until an appropriate udev rule is installed.

### NixOS (flakes)

Add the repo as an input — no files to copy, the flake carries both the module
and the driver:

```nix
# flake.nix
{
  inputs.idcool-display.url = "github:relf108/idcooling-temp-display-linux";
  # pass inputs through to your nixosSystem (specialArgs / module args), then
  # in a module:  imports = [ inputs.idcool-display.nixosModules.default ];
}
```

```nix
# configuration.nix (or any imported module)
services.idcool-display.enable = true;
services.idcool-display.metric = "temp";   # or "usage" / "freq"
```

`nixos-rebuild switch` and it runs at boot.

### NixOS (without flakes)

```nix
{ ... }:
let
  idcool = builtins.fetchTarball
    "https://github.com/relf108/idcooling-temp-display-linux/archive/main.tar.gz";
in
{
  imports = [ "${idcool}/nix/idcool-display.nix" ];
  services.idcool-display.enable = true;
}
```

### Other distributions (systemd)

The repository also includes a conventional systemd unit and udev rule:

```bash
sudo install -m0755 idcool_display.py /usr/local/bin/idcool-display
sudo install -m0644 udev/99-idcooling-temp-display.rules /etc/udev/rules.d/
sudo install -m0644 systemd/idcool-display.service /etc/systemd/system/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
sudo systemctl daemon-reload
sudo systemctl enable --now idcool-display.service
```

To show a different metric, change `--metric` in the service file, then run
`sudo systemctl daemon-reload && sudo systemctl restart idcool-display`.

The udev rule creates a stable `/dev/idcool` symlink and grants group access for
manual testing. The daemon still resolves the hidraw node by USB VID:PID and
verifies the opened device, so it does not rely on that symlink.

#### Optional Fedora example

An optional least-privilege setup using a dedicated service account is available
under [`contrib/fedora/`](contrib/fedora/README.md). It is independent of the
default systemd files and does not change the NixOS setup. Use one setup or the
other, not both: their udev rules share the installed filename
`99-idcooling-temp-display.rules` but grant access to different groups
(`plugdev` in the default setup, `idcool-display` in the Fedora setup). Each
service must be installed with its matching rule.

## Troubleshooting

- **`... display (1A86:E317) not found`** — the cooler is not plugged in or did
  not enumerate as the expected USB HID device. Confirm it with
  `lsusb -d 1a86:e317`.
- **Permission denied on `/dev/hidraw*`** — manual runs need `sudo` unless your
  user belongs to the group selected by the installed udev rule. The default
  systemd unit runs as root; the optional Fedora unit instead uses its dedicated
  `idcool-display` account and matching udev rule.
- **No supported CPU temperature sensor found** — pass
  `--temp-path /sys/class/hwmon/hwmonN/tempM_input` after identifying the CPU
  package sensor with `sensors` and the `*/name` / `temp*_label` files. This
  explicit selection avoids accidentally displaying a non-CPU sensor.
- **Screen goes blank after suspend** — `systemctl restart idcool-display`
  (the service also auto-restarts on failure).

## Files

| Path | What |
|------|------|
| `idcool_display.py` | the driver (stdlib only, CLI) |
| `flake.nix` | flake exposing the NixOS module + a `nix run` package |
| `PROTOCOL.md` | full HID protocol spec + how it was decoded |
| `udev/99-idcooling-temp-display.rules` | `/dev/idcool` symlink + group access |
| `systemd/idcool-display.service` | standard systemd unit |
| `contrib/fedora/` | optional unprivileged Fedora/systemd setup |
| `nix/idcool-display.nix` | NixOS module |
| `tests/test_idcool_display.py` | standard-library unit tests |

## Verified hardware

This is not an exhaustive list of supported models, only devices reported to
work with this driver.

- ID-COOLING FROZN A410 TD

## Credits

Protocol reverse-engineered from the official ID-COOLING Temp Display app by
reading its (Electron/JavaScript) source — no Windows or Wine required; see
[`PROTOCOL.md`](PROTOCOL.md) for the exact steps so you can verify it yourself.

Not affiliated with or endorsed by ID-COOLING. Trademarks belong to their
owners. Use at your own risk.

## License

MIT — see [`LICENSE`](LICENSE).
