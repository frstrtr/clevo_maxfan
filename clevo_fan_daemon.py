#!/usr/bin/env python3
"""Clevo fan binary controller — overrides BIOS curve with on/off behaviour.

Polls nvidia-smi every CLEVO_FAN_POLL_SECONDS. State machine with hysteresis:

  LOW  state → fan duty = CLEVO_FAN_LOW_DUTY  (default 60 %, the EC's
               minimum settable; closest to "quiet" without giving control
               back to BIOS)
  HIGH state → fan duty = CLEVO_FAN_HIGH_DUTY (default 100 %)

Transitions:
  LOW → HIGH when (GPU util ≥ CLEVO_FAN_HIGH_UTIL for ≥ CLEVO_FAN_HIGH_DEBOUNCE s)
                OR (GPU temp ≥ CLEVO_FAN_HIGH_TEMP °C immediately)
  HIGH → LOW when (GPU util ≤ CLEVO_FAN_LOW_UTIL for ≥ CLEVO_FAN_LOW_DEBOUNCE s)
                AND GPU temp ≤ CLEVO_FAN_LOW_TEMP °C

Runs as root (the CLI uses inb/outb to talk to the EC). All thresholds are
env-tunable so you can adjust without redeploying.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from typing import Optional, Tuple


CLEVO_FAN_CLI = os.environ.get("CLEVO_FAN_CLI", "/usr/local/bin/clevo-fan-cli")

# Defaults are SURVIVAL-tuned (server-room context, noise irrelevant). The
# optimization target is component longevity:
#   - Lower temps → exponentially longer GPU/VRAM/CPU life (Arrhenius)
#   - Fewer up/down transitions → less BGA / solder thermal-cycling stress
#   - Steady high fan under load → better than letting silicon bake during debounce
#
# Trade-off accepted: fan bearing wears faster from sustained-100% RPM than
# from BIOS-curve, but the user is explicitly OK with that — GPU/CPU silicon
# is more expensive and harder to replace than the fans.
#
# The Clevo P77xDM2-G has TWO fans (one CPU, one GPU) with heat-pipe-coupled
# heatsinks. The CLI now sets BOTH fan planes; the daemon ORs CPU and GPU
# signals because either component's heat affects the other through the
# shared thermal mass.

# GPU triggers (nvidia-smi-driven)
GPU_HIGH_UTIL      = int(os.environ.get("CLEVO_FAN_GPU_HIGH_UTIL",     "15"))
GPU_HIGH_TEMP      = int(os.environ.get("CLEVO_FAN_GPU_HIGH_TEMP",     "60"))
GPU_LOW_UTIL       = int(os.environ.get("CLEVO_FAN_GPU_LOW_UTIL",       "5"))
GPU_LOW_TEMP       = int(os.environ.get("CLEVO_FAN_GPU_LOW_TEMP",      "45"))

# CPU triggers (/proc/stat + /sys/class/thermal/*-driven). i7-6700K idles
# warm (~55-60°C) so LOW_TEMP must accommodate idle baseline. HIGH triggers
# engage on real work — sustained util above background-noise or temp
# crossing into thermal-stress territory.
CPU_HIGH_UTIL      = int(os.environ.get("CLEVO_FAN_CPU_HIGH_UTIL",     "40"))
CPU_HIGH_TEMP      = int(os.environ.get("CLEVO_FAN_CPU_HIGH_TEMP",     "70"))
CPU_LOW_UTIL       = int(os.environ.get("CLEVO_FAN_CPU_LOW_UTIL",      "15"))
CPU_LOW_TEMP       = int(os.environ.get("CLEVO_FAN_CPU_LOW_TEMP",      "65"))

# Back-compat: the original GPU-only env vars still work if set.
def _legacy(name, fallback):
    v = os.environ.get(name)
    return int(v) if v is not None else fallback
GPU_HIGH_UTIL = _legacy("CLEVO_FAN_HIGH_UTIL", GPU_HIGH_UTIL)
GPU_HIGH_TEMP = _legacy("CLEVO_FAN_HIGH_TEMP", GPU_HIGH_TEMP)
GPU_LOW_UTIL  = _legacy("CLEVO_FAN_LOW_UTIL",  GPU_LOW_UTIL)
GPU_LOW_TEMP  = _legacy("CLEVO_FAN_LOW_TEMP",  GPU_LOW_TEMP)

HIGH_DEBOUNCE      = float(os.environ.get("CLEVO_FAN_HIGH_DEBOUNCE",  "1"))
LOW_DEBOUNCE       = float(os.environ.get("CLEVO_FAN_LOW_DEBOUNCE", "180"))
LOW_DUTY           = int(os.environ.get("CLEVO_FAN_LOW_DUTY",        "60"))
HIGH_DUTY          = int(os.environ.get("CLEVO_FAN_HIGH_DUTY",      "100"))
POLL_SECONDS       = float(os.environ.get("CLEVO_FAN_POLL_SECONDS",   "1"))
# Minimum time to stay in HIGH state after entering it — even if util drops
# immediately. Prevents micro-cycling on transient idle dips between
# inference batches (the worst pattern for BGA solder fatigue).
MIN_TIME_AT_HIGH   = float(os.environ.get("CLEVO_FAN_MIN_TIME_AT_HIGH", "60"))


_STOP = False


def _on_signal(signum, _frame):
    global _STOP
    _STOP = True
    logging.info("signal %d received — exiting at next poll", signum)


def read_gpu() -> Optional[Tuple[int, int]]:
    """Returns (util_pct, temp_c). None on any nvidia-smi failure — caller
    keeps last state and retries next poll."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=4,
        ).decode("utf-8", errors="replace").strip()
        # Multi-GPU box would return multiple rows; we take the first (Clevo
        # has one discrete GPU, but be defensive).
        first = out.splitlines()[0].strip()
        util_s, temp_s = (v.strip() for v in first.split(","))
        return int(util_s), int(temp_s)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, ValueError, IndexError) as exc:
        logging.warning("nvidia-smi read failed: %s", exc)
        return None


# /proc/stat-based CPU util sampler. Keeps the last snapshot so we can
# compute the busy% over the poll interval.
_LAST_CPU_SNAPSHOT: Optional[Tuple[int, int]] = None  # (idle, total)


def _read_proc_stat_cpu_aggregate() -> Optional[Tuple[int, int]]:
    """Returns (idle_jiffies, total_jiffies) from /proc/stat's 'cpu ' line.
    Idle includes both `idle` and `iowait` (we consider iowait as idle
    since the CPU isn't computing during it)."""
    try:
        with open("/proc/stat", "r") as f:
            first = f.readline().split()
        if not first or first[0] != "cpu":
            return None
        nums = [int(x) for x in first[1:]]
        # fields: user nice system idle iowait irq softirq steal guest guest_nice
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        return idle, total
    except (OSError, ValueError):
        return None


def read_cpu_util() -> Optional[int]:
    """Returns CPU util % (0-100) computed over the time since the last
    call. First call returns None (need two snapshots for a delta)."""
    global _LAST_CPU_SNAPSHOT
    snap = _read_proc_stat_cpu_aggregate()
    if snap is None:
        return None
    if _LAST_CPU_SNAPSHOT is None:
        _LAST_CPU_SNAPSHOT = snap
        return None
    prev_idle, prev_total = _LAST_CPU_SNAPSHOT
    cur_idle, cur_total = snap
    _LAST_CPU_SNAPSHOT = snap
    didle = cur_idle - prev_idle
    dtotal = cur_total - prev_total
    if dtotal <= 0:
        return None
    busy_pct = max(0, min(100, int(round((1.0 - didle / dtotal) * 100))))
    return busy_pct


# Cached list of coretemp/x86_pkg_temp thermal zones (resolved once).
_CPU_TEMP_ZONES: Optional[list] = None


def _discover_cpu_temp_zones() -> list:
    """Scan /sys/class/thermal/thermal_zone*/type for CPU-relevant zones.
    Matches: coretemp, x86_pkg_temp, cpu_thermal, k10temp. Returns paths
    to the temp file for each matching zone. Empty list if no CPU zones
    found (we'll fall back to the EC's CPU reg via the CLI, slower)."""
    paths = []
    for zone in sorted(os.listdir("/sys/class/thermal")):
        if not zone.startswith("thermal_zone"):
            continue
        type_path = f"/sys/class/thermal/{zone}/type"
        temp_path = f"/sys/class/thermal/{zone}/temp"
        try:
            with open(type_path) as f:
                t = f.read().strip().lower()
            if any(key in t for key in ("coretemp", "x86_pkg_temp", "cpu_thermal", "k10temp", "pkg-temp")):
                paths.append(temp_path)
        except OSError:
            continue
    return paths


def read_cpu_temp() -> Optional[int]:
    """Returns the max CPU temp (°C) across all coretemp zones. None on
    any read failure."""
    global _CPU_TEMP_ZONES
    if _CPU_TEMP_ZONES is None:
        _CPU_TEMP_ZONES = _discover_cpu_temp_zones()
        if _CPU_TEMP_ZONES:
            logging.info("CPU temp zones: %s", _CPU_TEMP_ZONES)
        else:
            logging.warning("no CPU temp zones found in /sys/class/thermal; CPU temp triggers disabled")
    if not _CPU_TEMP_ZONES:
        return None
    max_temp = None
    for p in _CPU_TEMP_ZONES:
        try:
            with open(p) as f:
                # /sys exposes milli-celsius
                t_mc = int(f.read().strip())
            t = t_mc // 1000
            if max_temp is None or t > max_temp:
                max_temp = t
        except (OSError, ValueError):
            continue
    return max_temp


def set_duty(pct: int) -> bool:
    """Calls clevo-fan-cli with the given % (60..100). Returns True on success."""
    try:
        subprocess.run(
            [CLEVO_FAN_CLI, str(pct)],
            check=True,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logging.error("set_duty(%d) failed: %s", pct, getattr(exc, "stderr", b"") or exc)
        return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info(
        "clevo_fan_daemon starting (dual-fan, heat-pipe-coupled): "
        "GPU HIGH at util>=%d%% or temp>=%d°C, LOW at util<=%d%% AND temp<=%d°C; "
        "CPU HIGH at util>=%d%% or temp>=%d°C, LOW at util<=%d%% AND temp<=%d°C; "
        "debounce up=%.1fs / down=%.1fs; min-time-high=%.1fs; "
        "duties low=%d%% high=%d%%; poll %.1fs",
        GPU_HIGH_UTIL, GPU_HIGH_TEMP, GPU_LOW_UTIL, GPU_LOW_TEMP,
        CPU_HIGH_UTIL, CPU_HIGH_TEMP, CPU_LOW_UTIL, CPU_LOW_TEMP,
        HIGH_DEBOUNCE, LOW_DEBOUNCE, MIN_TIME_AT_HIGH,
        LOW_DUTY, HIGH_DUTY, POLL_SECONDS,
    )
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Initial state: low. Set duty once so the EC matches our model.
    state = "low"
    last_duty = None
    high_since: Optional[float] = None
    low_since: Optional[float] = None
    high_entry_time: Optional[float] = None  # for MIN_TIME_AT_HIGH guarantee
    if set_duty(LOW_DUTY):
        last_duty = LOW_DUTY

    while not _STOP:
        gpu_reading = read_gpu()
        cpu_util = read_cpu_util()
        cpu_temp = read_cpu_temp()

        # If GPU read failed, we still proceed if CPU side is healthy — but
        # if everything failed, sleep and retry.
        if gpu_reading is None and cpu_util is None and cpu_temp is None:
            time.sleep(POLL_SECONDS)
            continue

        gpu_util, gpu_temp = (gpu_reading if gpu_reading is not None else (None, None))
        now = time.monotonic()

        # Build per-source trigger flags. Missing reads contribute neither a
        # HIGH trigger nor a LOW satisfaction — we treat them as unknown.
        gpu_high = (gpu_util is not None and gpu_util >= GPU_HIGH_UTIL) \
                or (gpu_temp is not None and gpu_temp >= GPU_HIGH_TEMP)
        cpu_high = (cpu_util is not None and cpu_util >= CPU_HIGH_UTIL) \
                or (cpu_temp is not None and cpu_temp >= CPU_HIGH_TEMP)
        gpu_low_ok = (gpu_util is None or gpu_util <= GPU_LOW_UTIL) \
                 and (gpu_temp is None or gpu_temp <= GPU_LOW_TEMP)
        cpu_low_ok = (cpu_util is None or cpu_util <= CPU_LOW_UTIL) \
                 and (cpu_temp is None or cpu_temp <= CPU_LOW_TEMP)

        # OR-fuse: either component drives HIGH; both must be quiet to drop LOW.
        high_trigger_now = gpu_high or cpu_high
        low_eligible_now = gpu_low_ok and cpu_low_ok

        # Immediate emergency: GPU or CPU temp already over its HIGH ceiling.
        immediate_temp_emergency = (
            (gpu_temp is not None and gpu_temp >= GPU_HIGH_TEMP)
            or (cpu_temp is not None and cpu_temp >= CPU_HIGH_TEMP)
        )

        # For log readability, pre-format the reading line.
        def _fmt_reading():
            return "gpu(util=%s temp=%s) cpu(util=%s temp=%s)" % (
                f"{gpu_util}%" if gpu_util is not None else "?",
                f"{gpu_temp}°C" if gpu_temp is not None else "?",
                f"{cpu_util}%" if cpu_util is not None else "?",
                f"{cpu_temp}°C" if cpu_temp is not None else "?",
            )

        if state == "low":
            if immediate_temp_emergency:
                state = "high"
                high_since = None; low_since = None
                high_entry_time = now
                if set_duty(HIGH_DUTY):
                    last_duty = HIGH_DUTY
                    logging.info("low → high: TEMP EMERGENCY %s", _fmt_reading())
            elif high_trigger_now:
                if high_since is None:
                    high_since = now
                elif now - high_since >= HIGH_DEBOUNCE:
                    state = "high"
                    high_since = None; low_since = None
                    high_entry_time = now
                    if set_duty(HIGH_DUTY):
                        last_duty = HIGH_DUTY
                        logging.info("low → high: %s (debounced %.1fs)", _fmt_reading(), HIGH_DEBOUNCE)
            else:
                high_since = None
        else:  # state == "high"
            # Survival rule: stay in HIGH for at least MIN_TIME_AT_HIGH after
            # transitioning up, even if util drops to 0% immediately. Prevents
            # high-frequency thermal cycling on bursty inference workloads
            # (the worst pattern for BGA solder fatigue).
            min_time_remaining = (
                MIN_TIME_AT_HIGH - (now - high_entry_time)
                if high_entry_time is not None else 0
            )
            if min_time_remaining > 0:
                # Reset the LOW debounce — we're not going down yet, period.
                low_since = None
            elif low_eligible_now:
                if low_since is None:
                    low_since = now
                elif now - low_since >= LOW_DEBOUNCE:
                    state = "low"
                    high_since = None; low_since = None; high_entry_time = None
                    if set_duty(LOW_DUTY):
                        last_duty = LOW_DUTY
                        logging.info("high → low: %s (debounced %.1fs)", _fmt_reading(), LOW_DEBOUNCE)
            else:
                low_since = None

        time.sleep(POLL_SECONDS)

    logging.info("clevo_fan_daemon stopping (last duty=%s, state=%s)", last_duty, state)
    # Deliberately do NOT change duty on exit — leaves whatever was last
    # commanded so the operator can pick up where we left off. If you want
    # a guaranteed "back to a known state on stop", set CLEVO_FAN_LOW_DUTY
    # and uncomment the line below.
    # set_duty(LOW_DUTY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
