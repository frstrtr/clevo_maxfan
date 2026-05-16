# clevo_maxfan

Headless-friendly fan controller for Clevo laptops/servers, intended for 24×7
use where the BIOS auto-curve runs the GPU/CPU too hot under sustained load.
Two operating modes:

1. **Load-aware daemon** (recommended) — polls `nvidia-smi` and the kernel
   thermal zones; flips fans between 60 % (idle baseline) and 100 % (under
   GPU **or** CPU load) with hysteresis tuned for component longevity, not
   noise. Survives the worst pattern for BGA solder fatigue (high-frequency
   thermal cycling) via a configurable minimum-dwell-at-HIGH.
2. **Legacy 5-min force-max timer** — sets fan to 100 % every 5 minutes
   regardless of load. Works on any chassis with no extra deps (no `nvidia-smi`
   needed). The original behaviour of this repo.

The CLI now writes **both fan planes** (CPU fan_id `0x01` + GPU fan_id `0x02`)
on every `set` call. Single-plane chassis (older Clevos, e.g. P775DM) silently
ignore the second write — harmless. Dual-plane chassis (P77xDM2-G class) gain
proper symmetric cooling without the daemon needing per-fan addressing.

## Tested chassis

| Chassis | CPU | GPU | Fan planes | Status |
|---|---|---|---|---|
| Clevo P77xDM2-G | i7-6700K | GTX 1070 | 2 (CPU + GPU) | ✅ verified — daemon flips both fans correctly |
| Clevo P775DM | i7-6700K | GTX 1070 | 1 (combined) | ✅ verified — second-plane write is a harmless no-op |

The EC protocol is shared across most Clevo chassis from the
P15x/P17x/P65x/P67x/P77x generations onward. File an issue with
`clevo-fan-cli read` output before/after if you test on another model.

## Build + install

```bash
sudo apt-get install -y build-essential       # gcc + make
git clone https://github.com/frstrtr/clevo_maxfan.git
cd clevo_maxfan
make
sudo make install
```

`make install` lays down:
- `/usr/local/bin/clevo-fan-cli` (setuid root — required for direct port IO)
- `/usr/local/bin/clevo_fan_daemon.py` (the load-aware daemon)
- `/etc/systemd/system/clevo-fan-daemon.service` (the daemon unit)
- `/etc/systemd/system/clevo-fan-max.service` + `.timer` (legacy unit + retimer)

Pick exactly **one** of the two enable paths below.

---

## Path A — load-aware daemon (recommended)

Requires `nvidia-smi` available on `PATH` (i.e., NVIDIA proprietary driver
installed). On a fresh Ubuntu 24.04+ install: `sudo ubuntu-drivers install --gpgpu`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now clevo-fan-daemon.service
journalctl -u clevo-fan-daemon.service -f      # watch transitions
```

### What it does

Polls every second:
- GPU util + temp via `nvidia-smi --query-gpu=utilization.gpu,temperature.gpu`
- CPU util via `/proc/stat` deltas (no extra deps)
- CPU temp via `/sys/class/thermal/*` matching `coretemp|x86_pkg_temp|k10temp|cpu_thermal`

State machine with hysteresis:
- **LOW → HIGH** when (GPU util ≥ `GPU_HIGH_UTIL`) **OR** (GPU temp ≥ `GPU_HIGH_TEMP`)
  **OR** (CPU util ≥ `CPU_HIGH_UTIL`) **OR** (CPU temp ≥ `CPU_HIGH_TEMP`).
  Up-direction debounce is 1 s. A temp already at the HIGH ceiling bypasses
  debounce as an immediate emergency.
- **HIGH → LOW** when **all four** sources are quiet (GPU util ≤ `GPU_LOW_UTIL`
  AND GPU temp ≤ `GPU_LOW_TEMP` AND CPU util ≤ `CPU_LOW_UTIL` AND CPU temp ≤
  `CPU_LOW_TEMP`) sustained for `LOW_DEBOUNCE` (default 180 s = 3 min) — and only
  after `MIN_TIME_AT_HIGH` (default 60 s) has elapsed since entering HIGH.

Single-fan-plane chassis still benefit from the daemon — the daemon issues one
`set` call per duty change, and the CLI dual-write is harmless to the unused
plane.

### Tuning (no rebuild needed)

Edit `/etc/systemd/system/clevo-fan-daemon.service`, change the `Environment=`
lines, then:

```bash
sudo systemctl daemon-reload && sudo systemctl restart clevo-fan-daemon
```

| Env var | Default | What it controls |
|---|---|---|
| `CLEVO_FAN_GPU_HIGH_UTIL`   | `15` | GPU util % that triggers HIGH |
| `CLEVO_FAN_GPU_HIGH_TEMP`   | `60` | GPU °C that triggers HIGH (also emergency) |
| `CLEVO_FAN_GPU_LOW_UTIL`    | `5`  | GPU util % required for LOW return |
| `CLEVO_FAN_GPU_LOW_TEMP`    | `45` | GPU °C required for LOW return |
| `CLEVO_FAN_CPU_HIGH_UTIL`   | `40` | CPU util % that triggers HIGH |
| `CLEVO_FAN_CPU_HIGH_TEMP`   | `70` | CPU °C that triggers HIGH (also emergency) |
| `CLEVO_FAN_CPU_LOW_UTIL`    | `15` | CPU util % required for LOW return |
| `CLEVO_FAN_CPU_LOW_TEMP`    | `65` | CPU °C required for LOW return (accounts for idle baseline) |
| `CLEVO_FAN_HIGH_DEBOUNCE`   | `1`  | seconds before LOW→HIGH (other than emergency) |
| `CLEVO_FAN_LOW_DEBOUNCE`    | `180`| seconds before HIGH→LOW |
| `CLEVO_FAN_MIN_TIME_AT_HIGH`| `60` | min dwell in HIGH after entering — guarantees against thermal cycling |
| `CLEVO_FAN_LOW_DUTY`        | `60` | duty % when LOW (EC minimum) |
| `CLEVO_FAN_HIGH_DUTY`       | `100`| duty % when HIGH |
| `CLEVO_FAN_POLL_SECONDS`    | `1`  | poll cadence |

### Why these defaults?

**Server-room context** — the goal is GPU/VRAM/CPU **silicon longevity**, not
noise. Arrhenius: each −10 °C in average junction temperature roughly doubles
expected silicon life. The trade is fan-bearing wear: running at sustained
high RPM under load eats fan bearings faster than a BIOS auto-curve would. A
fan is much cheaper than a GPU.

The `MIN_TIME_AT_HIGH=60` knob is the key reliability lever. Thermal cycling
(many LOW↔HIGH transitions per minute) is what fatigues BGA solder joints far
more than a steady high temperature. Inference workloads tend to be bursty —
without this floor, you'd cycle dozens of times per minute. With it, once HIGH
is entered, fans stay HIGH for at least a minute regardless of util drops.

---

## Path B — legacy 5-min force-max timer

Use this on chassis without an NVIDIA GPU (no `nvidia-smi`), or when you just
want "always max" with no smarts. This is the original behaviour of the repo.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now clevo-fan-max.timer
```

The timer fires once at boot (after 30 s) and then every 5 minutes. Each fire
runs the oneshot service which calls `clevo-fan-cli 100`. This catches the EC
silently dropping back to its auto-curve after suspend/resume, AC unplug, or
similar events.

```bash
systemctl list-timers clevo-fan-max.timer       # inspect
sudo systemctl stop clevo-fan-max.timer         # disable temporarily
sudo clevo-fan-cli 60                           # back toward the BIOS minimum
```

### ⚠ Don't run both paths together

If you enable both `clevo-fan-daemon.service` AND `clevo-fan-max.timer`, the
timer will overwrite the daemon's "LOW" duty every 5 minutes. Pick one.

---

## CLI usage

```bash
# read current temps + fan duty (only reports CPU-plane duty register;
# nvidia-smi for the real GPU core temp — see note below)
clevo-fan-cli read
# → cpu=65°C gpu=0°C fan_duty_raw=106 (42%)

# set fan duty (allowed range: 60-100 percent) — writes BOTH planes
sudo clevo-fan-cli 100
# → fan duty set to 100%
```

`gpu=0°C` from the EC register is normal on chassis where the GPU thermal
sensor isn't wired into the EC, or when the GPU is in deep-sleep / ASPM.
The daemon uses `nvidia-smi --query-gpu=temperature.gpu --format=csv` (NVML
via the driver) for the real GPU core temp regardless.

---

## Uninstall

```bash
sudo make uninstall
```

Disables both unit options, removes binaries + unit files.

---

## How it works

The Clevo embedded controller (EC) listens on legacy x86 IO ports `0x66`
(status/command) and `0x62` (data). To set fan duty:

1. `ioperm()` to gain port access (root).
2. Wait until the EC's input-buffer-full bit clears.
3. Issue command byte `0x99` (set-fan-duty), data byte `<fan_id>` (0x01 = CPU
   fan, 0x02 = GPU fan), data byte `<duty 0-255>`.

The CLI issues both fan_id writes in sequence. On single-plane chassis the
`0x02` write is silently ignored by the EC.

Reading temps + current duty uses the EC `0x80` read command against registers
`0x07` (CPU temp), `0xCD` (GPU temp), `0xCE` (current duty).

The full protocol was reverse-engineered by SkyLandTW; this project is a
minimal headless fork.

---

## Safety

- **Never run two programs that touch the EC over IO ports concurrently**
  (e.g. this tool plus `nbfc`, `tuxedofancontrol`, or another fan utility).
  Concurrent EC writes have undefined behaviour and the EC has no kernel-side
  arbitration.
- The setuid bit on the CLI binary is required for `ioperm()`. The program
  does at most two fan writes and exits — no background process, no
  privilege-escalation surface. If you'd rather avoid setuid, drop the bit
  (`chmod 0755`) and the daemon's systemd unit (which already runs as root)
  still works fine.
- The daemon does **not** revert fan duty on `SIGTERM`. Stopping the daemon
  leaves whatever it last commanded; reboot reverts to BIOS defaults.

---

## Changelog

- **2026-05-16** — Add load-aware daemon (`clevo_fan_daemon.py`) and
  `clevo-fan-daemon.service`. CLI now writes both fan planes (CPU `0x01` +
  GPU `0x02`) on every set. Survival-tuned defaults for server-room use.
- **2026-05-13** — Initial public release: CLI + 5-min force-max systemd
  service/timer.

## License

MIT — see [LICENSE](LICENSE). The original EC protocol implementation was
released by SkyLandTW under the Unlicense (public domain); this is a derivative
focused on headless / server use.
