#!/usr/bin/env python3
"""Narrow down why a second CMD_ARM write does not extend the deadline.

Observed so far (PL clock measures ~27.78 MHz, so TIMEOUT_MS=1000 -> 1.80 s):
  arm at t=0, arm at t=1.20 -> fault still at 1.80 s (no reload)

This test checks whether the reload works when the gate was disarmed first:
  arm (t=0), then at t=1.20: CTRL=0 (disarms) -> CTRL=ENABLE -> CMD_ARM
  - fault at ~3.00 s -> the reload works only while wd_armed was 0, i.e. the
                        countdown overwrites the reload on arm cycles
  - fault at ~1.80 s -> the reload never takes effect at all
Also reads back TIMEOUT_MS / CTRL and measures the clock over 5 s.
"""

import time

from pynq import Overlay
from pynq import MMIO

BITSTREAM = "/home/xilinx/safety_gate/system.bit"
BASE = 0x43C00000
RANGE = 0x1000

CTRL, TIMEOUT_MS, CMD_VX, CMD_VY, CMD_WZ, CMD_ARM = 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18
STATUS, HEARTBEAT = 0x28, 0x30

CTRL_ENABLE = 1 << 0
CTRL_CLEAR = 1 << 2
ST_F_TIMEOUT = 1 << 4


def poll_fault(r, limit):
    t = time.time()
    while time.time() - t < limit:
        if r.read(STATUS) & ST_F_TIMEOUT:
            return time.time() - t
        time.sleep(0.005)
    return None


def main():
    Overlay(BITSTREAM)
    r = MMIO(BASE, RANGE)

    hb1 = r.read(HEARTBEAT)
    t1 = time.time()
    time.sleep(5.0)
    hb2 = r.read(HEARTBEAT)
    dt = time.time() - t1
    print("clock over 5s: %d / %.4fs = %.3f MHz" % (hb2 - hb1, dt, (hb2 - hb1) / dt / 1e6))

    r.write(CTRL, CTRL_ENABLE | CTRL_CLEAR)
    r.write(TIMEOUT_MS, 1000)
    r.write(CMD_VX, 0)
    r.write(CMD_VY, 0)
    r.write(CMD_WZ, 0)
    print("readback: timeout_ms=%d ctrl=0x%x" % (r.read(TIMEOUT_MS), r.read(CTRL)))

    r.write(CMD_ARM, 1)
    t0 = time.time()
    time.sleep(1.20)
    print("t=%.3f before rearm: status=0x%x" % (time.time() - t0, r.read(STATUS)))

    # disarm -> enable -> arm
    r.write(CTRL, 0)
    dis = r.read(STATUS)
    r.write(CTRL, CTRL_ENABLE)
    en = r.read(STATUS)
    r.write(CMD_ARM, 1)
    re = r.read(STATUS)
    print("t=%.3f after disarm/re-enable/re-arm: dis=0x%x en=0x%x arm=0x%x"
          % (time.time() - t0, dis, en, re))

    fired = poll_fault(r, 3.0)
    print("fault at %s (3.00s => reload ok when disarmed, 1.80s => never reloads)"
          % ("None" if fired is None else "%.3fs" % (fired + 1.20)))
    r.write(CTRL, CTRL_CLEAR)


if __name__ == "__main__":
    main()
