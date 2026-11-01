#!/usr/bin/env python3
"""Decide whether a SECOND arm write actually reloads the watchdog deadline.

Measured facts from gate_probe.py:
  * PL clock is ~27.78 MHz (not the 50 MHz the RTL assumes), so the real
    deadline is 1.8x the programmed value.
  * one arm + TIMEOUT_MS=1000 fired at 1.801 s -> the first reload works.
  * re-arming every 50 ms with TIMEOUT_MS=200 still fired after ~0.36 s of
    real time, i.e. only the first reload counted.

Test A (decisive):
  TIMEOUT_MS = 1000  (real deadline 1.80 s)
  arm at t=0, arm again at t=1.20 s (before the deadline), then wait.
  - fault at ~1.80 s  -> the second arm did NOT reload
  - fault at ~3.00 s  -> the second arm DID reload

All commands are zero velocity, so nothing can move.
"""

import time

from pynq import Overlay
from pynq import MMIO

BITSTREAM = "/home/xilinx/safety_gate/system.bit"
BASE = 0x43C00000
RANGE = 0x1000

CTRL, TIMEOUT_MS, CMD_VX, CMD_VY, CMD_WZ, CMD_ARM = 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18
STATUS, STATS = 0x28, 0x2C

CTRL_ENABLE = 1 << 0
CTRL_CLEAR = 1 << 2
ST_F_TIMEOUT = 1 << 4


def wait_fault(r, t_start, limit):
    while time.time() - t_start < limit:
        if r.read(STATUS) & ST_F_TIMEOUT:
            return time.time() - t_start
        time.sleep(0.005)
    return None


def main():
    Overlay(BITSTREAM)
    r = MMIO(BASE, RANGE)

    # ---- test A -----------------------------------------------------------
    r.write(CTRL, CTRL_ENABLE | CTRL_CLEAR)
    r.write(TIMEOUT_MS, 1000)
    r.write(CMD_VX, 0)
    r.write(CMD_VY, 0)
    r.write(CMD_WZ, 0)
    r.write(CMD_ARM, 1)
    t0 = time.time()

    time.sleep(1.20)
    armed2 = bool(r.read(STATUS) & 0x2)  # ST_WD_OK, still armed?
    r.write(CMD_ARM, 1)
    st = r.read(STATUS)
    print("testA: armed again at t=%.3fs (wd_ok_before=%s) status=0x%x stats=0x%08x"
          % (time.time() - t0, armed2, st, r.read(STATS)))

    fired = wait_fault(r, t0, 4.0)
    print("testA: fault at t=%s  (1.80s => no reload, 3.00s => reload works)"
          % ("None" if fired is None else "%.3fs" % fired))
    print("testA: stats=0x%08x status=0x%x" % (r.read(STATS), r.read(STATUS)))

    # ---- test B: three arms, 1.2 s apart ----------------------------------
    r.write(CTRL, CTRL_ENABLE | CTRL_CLEAR)
    r.write(TIMEOUT_MS, 1000)
    r.write(CMD_ARM, 1)
    t0 = time.time()
    marks = []
    for _ in range(2):
        time.sleep(1.20)
        r.write(CMD_ARM, 1)
        marks.append(round(time.time() - t0, 3))
    fired = wait_fault(r, t0, 4.0)
    print("testB: arms at 0.0 + %s, fault at %s" % (marks, "None" if fired is None else "%.3fs" % fired))
    print("testB: stats=0x%08x status=0x%x" % (r.read(STATS), r.read(STATUS)))
    r.write(CTRL, CTRL_CLEAR)


if __name__ == "__main__":
    main()
