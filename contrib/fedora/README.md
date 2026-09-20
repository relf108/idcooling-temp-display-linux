# Optional Fedora service setup

This directory provides an optional least-privilege alternative to the
repository's default systemd setup. It runs the driver as a dedicated
`idcool-display` system user rather than as root. Nothing here is required by
the Python driver or the NixOS module.

This setup and the default systemd/udev setup are mutually exclusive. Both
install a rule named `/etc/udev/rules.d/99-idcooling-temp-display.rules` and a
service named `/etc/systemd/system/idcool-display.service`. The default rule
grants access to `plugdev`; this rule grants access to `idcool-display`. When
switching setups, stop the service first, then replace both the rule and the
service with the matching pair. Restart the service after applying the new
configuration (`sudo systemctl restart idcool-display.service`).

Review the files, then install them with:

```bash
# Install the driver as a root-owned executable.
sudo install -D -m0755 idcool_display.py /usr/local/bin/idcool-display

# Define and create the dedicated system user and group.
sudo install -D -m0644 contrib/fedora/idcool-display.sysusers \
  /etc/sysusers.d/idcool-display.conf
sudo systemd-sysusers /etc/sysusers.d/idcool-display.conf

# Grant that group access to this USB device and install the matching service.
sudo install -D -m0644 contrib/fedora/99-idcooling-temp-display.rules \
  /etc/udev/rules.d/99-idcooling-temp-display.rules
sudo install -D -m0644 contrib/fedora/idcool-display.service \
  /etc/systemd/system/idcool-display.service

# Apply the new configuration and start the service.
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
sudo systemctl daemon-reload
sudo systemctl enable --now idcool-display.service
```

`systemd-sysusers` creates the unprivileged `idcool-display` user and matching
group. It is safe to run again because it leaves an existing account in place.

Verify the setup with:

```bash
getent passwd idcool-display
getent group idcool-display
ls -l /dev/idcool
sudo systemctl status idcool-display
sudo journalctl -u idcool-display -b
```

To remove this optional setup:

```bash
sudo systemctl disable --now idcool-display.service
sudo rm -f /etc/systemd/system/idcool-display.service
sudo rm -f /etc/udev/rules.d/99-idcooling-temp-display.rules
sudo rm -f /etc/sysusers.d/idcool-display.conf
sudo systemctl daemon-reload
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
```

The system account is intentionally not removed automatically, in case local
files or configuration still refer to its numeric user or group ID.
