# clevo_maxfan

Headless-friendly CLI fan controller for Clevo laptops, intended for 24x7 server
or workstation use where the BIOS auto-curve runs the GPU/CPU too hot under
sustained load.

- **One C file**, no dependencies beyond glibc (no GTK, no AppIndicator, no Mono).
- **Direct EC port IO** via `inb`/`outb` — same protocol as the original
  [clevo-indicator](https://github.com/SkyLandTW/clevo-indicator), stripped to
  CLI-only so it builds and runs on a fresh server install.
- Ships with a **systemd service + timer** that re-applies max fan duty every
  5 minutes (covers EC resets after suspend/resume or AC events).

## Tested chassis

| Chassis | CPU | GPU | Status |
|---|---|---|---|
| Clevo P775DM | i7-6700K | GTX 1070 (mobile / desktop class) | ✅ verified — drops GPU temp ~10°C under load |

The EC protocol is shared across most Clevo chassis from the P15x/P17x/P65x/P67x/P77x
generations onward. If your chassis uses the same EC firmware, this should work.
File an issue with `clevo-fan-cli read` output before / after if you test on
another model.

## Build + install

```bash
sudo apt-get install -y build-essential       # gcc + make
git clone https://github.com/frstrtr/clevo_maxfan.git
cd clevo_maxfan
make
sudo make install
```

Installs:
- `/usr/local/bin/clevo-fan-cli` (setuid root — required for direct port IO)
- `/etc/systemd/system/clevo-fan-max.service`
- `/etc/systemd/system/clevo-fan-max.timer`

## Usage

```bash
# read current temps + fan duty
clevo-fan-cli read
# → cpu=65°C gpu=0°C fan_duty_raw=106 (42%)

# set fan duty (allowed range: 60-100 percent)
sudo clevo-fan-cli 100
# → fan duty set to 100%
```

(`gpu=0°C` from the EC register is normal on chassis where the GPU thermal
sensor isn't wired into the EC — use `nvidia-smi --query-gpu=temperature.gpu
--format=csv` for the real GPU core temp.)

## Persistent max-fan via systemd

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now clevo-fan-max.timer
```

The timer fires once at boot (after 30 s) and then every 5 minutes. Each fire
runs the oneshot service which calls `clevo-fan-cli 100`. This catches the EC
silently dropping back to its auto-curve after suspend/resume, AC unplug, or
similar events.

```bash
# inspect timer
systemctl list-timers clevo-fan-max.timer

# disable temporarily
sudo systemctl stop clevo-fan-max.timer
sudo clevo-fan-cli 60   # back toward the BIOS minimum
```

## Uninstall

```bash
sudo make uninstall
```

## How it works

The Clevo embedded controller (EC) listens on legacy x86 IO ports `0x66`
(status/command) and `0x62` (data). To set fan duty:

1. `ioperm()` to gain port access (root).
2. Wait until the EC's input-buffer-full bit clears.
3. Issue command byte `0x99` (set-fan-duty), data byte `0x01` (fan plane id),
   data byte `<duty 0-255>`.

Reading temps + current duty uses the EC `0x80` read command against registers
`0x07` (CPU temp), `0xCD` (GPU temp), `0xCE` (current duty).

The full protocol was reverse-engineered by SkyLandTW; this project is a
minimal headless fork.

## Safety

- Never run two programs that touch the EC over IO ports concurrently
  (e.g. this tool plus `nbfc`, `tuxedofancontrol`, or another fan utility).
  Concurrent EC writes have undefined behaviour and the EC has no kernel-side
  arbitration.
- The setuid bit on the binary is required for `ioperm()`. The program does
  exactly one fan write and exits — no background process, no privilege
  escalation surface.
- If you'd rather avoid setuid, drop the bit (`chmod 0755`) and invoke via
  `sudo` instead.

## License

MIT — see [LICENSE](LICENSE). The original EC protocol implementation was
released by SkyLandTW under the Unlicense (public domain); this is a derivative
focused on headless / server use.
