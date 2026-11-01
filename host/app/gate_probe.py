#!/usr/bin/env python3
"""Probe the safety_gate watchdog timing directly on the board.

Register map (see fpga/src/safety_gate.v):
  0x00 VER, 0x04 CTRL, 0x08 TIMEOUT_MS, 0x0C CMD_VX, 0x10 CMD_VY,
  0x14 CMD_WZ, 0x18 CMD_ARM, 0x1C OUT_VX, 0x20 OUT_VY, 0x24 OUT_WZ,
  0x28 STATUS, 0x2C STATS, 0x30 HEARTBEAT

Measures:
  phase1  single arm, TIMEOUT_MS=1000 -> time until fault (expect ~1.0 s)
  phase2  TIMEOUT_MS=200, re-arm every 50 ms for 1 s -> fault must NOT fire,
          prints the status word every iteration with a timestamp
  phase3  stop feeding -> fault must fire within ~200 ms
All commanded velocities are zero, so nothing can move the robot.
"""

import time

from pynq import Overlay
from pynq import MMIO

BITSTREAM = "/home/xilinx/safety_gate/system.bit"
BASE = 0x43C00000
RANGE = 0x1000

VER = 0x00
CTRL = 0x04
TIMEOUT_MS = 0x08
CMD_VX = 0x0C
CMD_VY = 0x10
CMD_WZ = 0x14
CMD_ARM = 0x18
OUT_VX = 0x1C
STATUS = 0x28
STATS = 0x2C
HEARTBEAT = 0x30

CTRL_ENABLE = 1 << 0
CTRL_ESTOP = 1 << 1
CTRL_CLEAR = 1 << 2

ST_GATE_OPEN = 1 << 0
ST_WD_OK = 1 << 1
ST_ESTOP_N = 1 << 2
ST_ENABLE = 1 << 3
ST_F_TIMEOUT = 1 << 4
ST_F_ESTOP = 1 << 5


def s32(v):
    return v - (1 << 32) if v & 0x80000000 else v


def main():
    t_load = time.time()
    Overlay(BITSTREAM)
    print("overlay loaded in %.2fs" % (time.time() - t_load))
    r = MMIO(BASE, RANGE)
    print("version=0x%08x" % r.read(VER))

    # Measure the PL clock from the free-running counter.
    hb1 = r.read(HEARTBEAT)
    t0 = time.time()
    time.sleep(1.0)
    hb2 = r.read(HEARTBEAT)
    dt = time.time() - t0
    print("clock: heartbeat +%d in %.4fs -> %.2f MHz" % (hb2 - hb1, dt, (hb2 - hb1) / dt / 1e6))

    # ---- phase1: single arm, 1000 ms deadline -------------------------------
    r.write(CTRL, CTRL_ENABLE | CTRL_CLEAR)
    r.write(TIMEOUT_MS, 1000)
    r.write(CMD_VX, 0)
    r.write(CMD_VY, 0)
    r.write(CMD_WZ, 0)
    r.write(CMD_ARM, 1)
    t1 = time.time()
    fired1 = None
    while time.time() - t1 < 3.0:
        if r.read(STATUS) & ST_F_TIMEOUT:
            fired1 = time.time() - t1
            break
        time.sleep(0.005)
    print("phase1: fault at %.3fs (deadline 1.000s) status=0x%x"
          % (fired1 if fired1 is not None else -1.0, r.read(STATUS)))

    # ---- phase2: re-arm every 50 ms with a 200 ms deadline -----------------
    r.write(CTRL, CTRL_ENABLE | CTRL_CLEAR)
    r.write(TIMEOUT_MS, 200)
    t2 = time.time()
    fired2 = None
    for i in range(20):
        r.write(CMD_ARM, 1)
        time.sleep(0.05)
        st = r.read(STATUS)
        el = time.time() - t2
        if st & ST_F_TIMEOUT:
            fired2 = el
            print("phase2: FAULT at arm #%d t=%.3fs status=0x%x" % (i + 1, el, st))
            break
        if i % 4 == 3:
            print("phase2: t=%.3fs arm#%d status=0x%x out=%d stats=0x%08x"
                  % (el, i + 1, st, s32(r.read(OUT_VX)), r.read(STATS)))
    if fired2 is None:
        print("phase2: no fault while feeding, status=0x%x (expected)" % r.read(STATUS))

    # ---- phase3: stop feeding ---------------------------------------------
    t3 = time.time()
    fired3 = None
    while time.time() - t3 < 1.0:
        if r.read(STATUS) & ST_F_TIMEOUT:
            fired3 = time.time() - t3
            break
        time.sleep(0.005)
    print("phase3: fault %.3fs after last arm (deadline 0.200s)" % (fired3 if fired3 is not None else -1.0))

    stats = r.read(STATS)
    print("stats: timeouts=%d cmds=%d" % (stats & 0xFFFF, (stats >> 16) & 0xFFFF))
    r.write(CTRL, CTRL_CLEAR)
    print("final status=0x%x" % r.read(STATUS))


if __name__ == "__main__":
    main()
