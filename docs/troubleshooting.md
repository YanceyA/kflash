# Troubleshooting

Part of the kflash docs — back to the [README](../README.md).

### Device not found after flash

Wait a few seconds for USB re-enumeration. If the device does not reappear, check `ls /dev/serial/by-id/` manually. A power cycle of the MCU board may be needed.

### Permission denied on serial device

Ensure your user is in the `dialout` group: `sudo usermod -aG dialout $USER` (log out and back in for it to take effect).

### CAN interface not found

Verify your USB-to-CAN adapter is connected and the interface is up: `sudo ip link set can0 up type can bitrate 1000000` — see [CAN prerequisites](flashing.md#can-bus-support) for the full setup.

### Build fails with missing toolchain

Install the ARM cross-compiler: `sudo apt install gcc-arm-none-eabi`

### Sudo password prompts during flash

kflash uses `sudo` to stop/start the Klipper service. For unattended use, add a scoped sudoers entry for the two `systemctl` commands — see [Sudo Configuration](configuration.md#sudo-configuration). Avoid `NOPASSWD: ALL`.

### SSH disconnected mid-flash

kflash converts the hangup into a clean shutdown and restarts the Klipper service, but the flash itself is aborted — the device may be left in bootloader mode. Re-run the flash after reconnecting, and prefer running kflash inside `tmux`/`screen` so the session survives disconnects.

### kflash exits with Textual install instructions

If the `textual` package is missing (e.g. after a `git pull` without re-running `./install.sh`), kflash exits with install instructions: re-run `./install.sh`, or `pip install 'textual>=8.2,<9'` into the Python running kflash. (The hand-rolled legacy UI and its `--legacy-ui` flag were removed after the Textual UI reached parity; the flag is now accepted and ignored.)
