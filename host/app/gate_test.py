"""AXI sanity test for the safety_gate overlay on the ZYNQ-7020 (PYNQ).

Run on the board (192.168.1.101):
    echo xilinx | sudo -S /home/xilinx/safety_gate/run.sh \
        /home/xilinx/safety_gate/gate_test.py

What it proves
  1. the bitstream loads and the register block answers at 0x43C00000
  2. the measured PL clock matches the constant baked into the RTL (a wrong
     timebase would silently stretch the safety deadline)
  3. a commanded velocity reaches OUT_* only while the watchdog is armed
  4. re-arming every 50 ms keeps OUT_* alive, and each arm really reloads the
     watchdog (RELOADS counter must advance once per arm)
  5. when arming stops, OUT_* falls back to 0 within ~TIMEOUT_MS and a sticky
     fault bit is set (this is the property the whole safety argument rests on)
  6. writing CTRL.estop drops the output immediately

All commanded velocities are confined to OUT_* registers: this test never
touches the network, so it cannot move the robot.

Exit code 0 = all checks passed. Python 3.6 compatible (PYNQ 2.7 image).
"""
import sys
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
OUT_VY = 0x20
OUT_WZ = 0x24
STATUS = 0x28
STATS = 0x2C
HEARTBEAT = 0x30
RELOADS = 0x34
RAMP_STEPS = 0x38

CTRL_ENABLE = 1 << 0
CTRL_ESTOP = 1 << 1
CTRL_CLEAR = 1 << 2

ST_GATE_OPEN = 1 << 0
ST_WD_OK = 1 << 1
ST_ESTOP_N = 1 << 2
ST_ENABLE = 1 << 3
ST_F_TIMEOUT = 1 << 4
ST_F_ESTOP = 1 << 5

# Measured on this board: PS "41.6667 MHz" setting really delivers 27.778 MHz
# because the IO PLL runs at 666.667 MHz while PYNQ assumes 1000 MHz.
CLK_HZ_EXPECTED = 27777778
TIMEOUT_MS_TEST = 200

failures = []


def wait_out(regs, offset, want, timeout_ms=200):
    """Poll an OUT_* register until it equals *want*.

    Since cmd_shaper was integrated the output ramps (at most 20 mm/s per
    1 ms step), so a single read right after the command is expected to be
    smaller than the target.
    """
    deadline = time.time() + timeout_ms / 1000.0
    seen = None
    while time.time() < deadline:
        seen = s32(regs.read(offset))
        if seen == want:
            return True, seen
        time.sleep(0.001)
    return False, seen


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print("[{}] {} {}".format(status, name, detail))
    if not cond:
        failures.append(name)


def s32(v):
    """Interpret a 32-bit register value as signed."""
    return v - (1 << 32) if v & 0x80000000 else v


def measure_clock(regs, seconds=1.0):
    hb1 = regs.read(HEARTBEAT)
    t0 = time.time()
    time.sleep(seconds)
    hb2 = regs.read(HEARTBEAT)
    dt = time.time() - t0
    return (hb2 - hb1) / dt


def main():
    print("loading overlay {}".format(BITSTREAM))
    t0 = time.time()
    ol = Overlay(BITSTREAM)
    print("overlay loaded in {:.2f}s".format(time.time() - t0))
    regs = MMIO(BASE, RANGE)

    ver = regs.read(VER)
    check("version register", ver == 0x00010000, hex(ver))

    # --- timebase self-check ------------------------------------------------
    hz = measure_clock(regs)
    ratio = hz / CLK_HZ_EXPECTED
    check("clock matches RTL constant", 0.95 <= ratio <= 1.05,
          "{:.3f} MHz (expected {:.3f}, ratio {:.3f})".format(hz / 1e6, CLK_HZ_EXPECTED / 1e6, ratio))

    # --- clean slate: clear faults, disable, short timeout -------------------
    regs.write(CTRL, CTRL_CLEAR)
    regs.write(TIMEOUT_MS, TIMEOUT_MS_TEST)
    regs.write(CMD_VX, 0)
    regs.write(CMD_VY, 0)
    regs.write(CMD_WZ, 0)

    st = regs.read(STATUS)
    check("gate closed while disabled", not (st & ST_GATE_OPEN), hex(st))
    check("output zero while disabled", s32(regs.read(OUT_VX)) == 0)

    # --- enable + armed: command must appear on OUT --------------------------
    regs.write(CTRL, CTRL_ENABLE)
    regs.write(CMD_VX, 100)      # mm/s
    regs.write(CMD_VY, -50)      # mm/s
    regs.write(CMD_WZ, 300)      # mrad/s
    regs.write(CMD_ARM, 1)

    st = regs.read(STATUS)
    check("gate open after arm", bool(st & ST_GATE_OPEN), hex(st))
    check("wd ok bit", bool(st & ST_WD_OK), hex(st))
    # the shaper must not pass a big command through in one step
    first = s32(regs.read(OUT_VX))
    check("command not passed through instantly", first != 100, "first read {}".format(first))
    ok_vx, val_vx = wait_out(regs, OUT_VX, 100)
    ok_vy, val_vy = wait_out(regs, OUT_VY, -50)
    ok_wz, val_wz = wait_out(regs, OUT_WZ, 300)
    check("out_vx follows command after ramp", ok_vx, str(val_vx))
    check("out_vy follows command after ramp", ok_vy, str(val_vy))
    check("out_wz follows command after ramp", ok_wz, str(val_wz))

    # --- hardware ramp rate: 500 mm/s must arrive in DELTA-sized steps -------
    # The host polls slower than the 1 ms ramp tick (an AXI read plus sleep(1 ms)
    # takes ~1.3-2 ms), so a single sample gap may legitimately contain two or
    # three 20 mm/s steps.  Bound each observed jump by the time it actually
    # covered, and let the hardware step counter - not the host's sampling rate -
    # prove the ramp: a jump straight to the target moves 400 mm/s while the
    # counter advances by one, so the counter must match the travel exactly.
    steps_before = regs.read(RAMP_STEPS)
    regs.write(CMD_VX, 500)
    samples = []
    stamps = []
    t_ramp = time.perf_counter()
    while time.perf_counter() - t_ramp < 0.05:
        samples.append(s32(regs.read(OUT_VX)))
        stamps.append(time.perf_counter())
        time.sleep(0.001)
    gaps = [(b - a, (t1 - t0) * 1000.0)
            for a, b, t0, t1 in zip(samples, samples[1:], stamps, stamps[1:])
            if b != a]
    biggest = max([d for d, _ in gaps], default=0)
    widest = max([dt for _, dt in gaps], default=0.0)
    allowed = 20 * max(1, int(widest) + 1)
    steps_after = regs.read(RAMP_STEPS)
    expected_steps = (500 - 100) // 20
    check("ramp reached 500 mm/s", samples[-1] == 500, str(samples[-1]))
    check("ramp step bounded by DELTA", 0 < biggest <= allowed,
          "max {} mm/s over a {:.1f} ms sample gap (allowed {})".format(
              biggest, widest, allowed))
    check("ramp step counter matches travel", steps_after - steps_before == expected_steps,
          "{} steps for 400 mm/s, expected {}".format(
              steps_after - steps_before, expected_steps))
    regs.write(CMD_VX, 100)
    wait_out(regs, OUT_VX, 100)

    # --- keep feeding for 1 s: output must stay put -------------------------
    reloads0 = regs.read(RELOADS) & 0xFFFF
    feeds = 0
    t_last_arm = time.time()
    deadline = time.time() + 1.0
    while time.time() < deadline:
        regs.write(CMD_ARM, 1)
        t_last_arm = time.time()
        feeds += 1
        time.sleep(0.05)
    st = regs.read(STATUS)
    reloads1 = regs.read(RELOADS) & 0xFFFF
    check("no timeout while feeding", not (st & ST_F_TIMEOUT), hex(st))
    ok_feed, val_feed = wait_out(regs, OUT_VX, 100)
    check("output still following", ok_feed, str(val_feed))
    check("every arm reloaded the watchdog", (reloads1 - reloads0) == feeds,
          "reloads +{} feeds {}".format(reloads1 - reloads0, feeds))

    # --- stop feeding: watchdog must cut the output within TIMEOUT_MS -------
    # The deadline restarts at the LAST arm write, so measure from there.
    t_stop = time.time()
    gap_ms = (t_stop - t_last_arm) * 1000.0
    timeout_seen = False
    while time.time() - t_stop < 1.0:
        st = regs.read(STATUS)
        if st & ST_F_TIMEOUT:
            timeout_seen = True
            break
        time.sleep(0.002)
    elapsed_ms = (time.time() - t_last_arm) * 1000.0
    check("timeout fault after arming stops", timeout_seen,
          "after {:.1f} ms from the last arm ({:.1f} ms after the poll started)"
          .format(elapsed_ms, gap_ms))
    check("timeout close to programmed value",
          timeout_seen and 0.7 * TIMEOUT_MS_TEST <= elapsed_ms <= 1.6 * TIMEOUT_MS_TEST,
          "{:.1f} ms vs {} ms".format(elapsed_ms, TIMEOUT_MS_TEST))
    check("output zero after timeout", s32(regs.read(OUT_VX)) == 0, str(s32(regs.read(OUT_VX))))
    stats = regs.read(STATS)
    timeout_count = stats & 0xFFFF
    cmd_count = (stats >> 16) & 0xFFFF
    check("timeout counter incremented", timeout_count >= 1, "timeouts={} cmds={}".format(timeout_count, cmd_count))
    check("command counter incremented", cmd_count == feeds + 1, "timeouts={} cmds={}".format(timeout_count, cmd_count))

    # --- sticky fault must survive until explicitly cleared -----------------
    regs.write(CMD_ARM, 1)
    st = regs.read(STATUS)
    check("fault stays until cleared", bool(st & ST_F_TIMEOUT), hex(st))
    check("output stays zero while faulted", s32(regs.read(OUT_VX)) == 0)
    check("gate closed while faulted", not (st & ST_GATE_OPEN), hex(st))

    # --- clear fault, re-arm: output returns ---------------------------------
    regs.write(CTRL, CTRL_ENABLE | CTRL_CLEAR)
    regs.write(CMD_VX, -25)
    regs.write(CMD_ARM, 1)
    ok_back, val_back = wait_out(regs, OUT_VX, -25)
    check("output returns after clear (ramped)", ok_back, str(val_back))

    # --- e-stop -------------------------------------------------------------
    regs.write(CTRL, CTRL_ENABLE | CTRL_ESTOP)
    check("output zero on estop", s32(regs.read(OUT_VX)) == 0, str(s32(regs.read(OUT_VX))))
    st = regs.read(STATUS)
    # CTRL.estop is a software level (bit2 of STATUS reads 0 while it is set);
    # the sticky ST_F_ESTOP bit is reserved for the hardware estop_in pin.
    check("estopped status", not (st & ST_ESTOP_N), hex(st))

    # leave the block disabled and neutral
    regs.write(CTRL, 0)
    regs.write(CMD_VX, 0)
    regs.write(CMD_VY, 0)
    regs.write(CMD_WZ, 0)

    print("")
    if failures:
        print("FAILED CHECKS ({}): {}".format(len(failures), ", ".join(failures)))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
