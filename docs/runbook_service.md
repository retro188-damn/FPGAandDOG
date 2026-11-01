# Runbook: resident PL bridge service (`pl_service.py`)

Owner: 上位机/FPGA 协同链路
Last verified: 2026-10-26 (installed on the board; boot-persistence and dog test pending)

## 1. Why a service

The one-shot `gate_bridge.py` pays **4–5 s per invocation**: SSH handshake,
`sudo` password check, Python + `import pynq`, and an overlay load. That is fine
for a bench test and fatal for a voice-driven demo ("say the words, dog moves").

`pl_service.py` loads the overlay **once**, then runs a 50 Hz loop forever:
commands arrive on UDP, gated frames leave on UDP.

```
notebook                                  ZYNQ-7020 (this service)
  │  UDP 43902  {"vx":100,...}  ────────▶  ├─ write CMD_* + ARM
  │                                        ├─ read OUT_* (gated)
  │                                        └─ UDP ──▶ 192.168.1.120:43893
  │  UDP 43903  {status, p50/p99}  ◀──────┘
```

## 2. Ports and protocol

| Port | Direction | Payload |
|---|---|---|
| 43902 | notebook → board | JSON command, one datagram per command |
| 43903 | board → notebook | JSON status, 10 Hz (sent to the configured `--status-peer` and to whoever last commanded) |
| 43900 | board/PC ← 103 | state tee (separate service, not this one) |
| 43901 | bridge → sink | bench-only test sink |

Command datagrams:

```json
{"vx": 100, "vy": 0, "wz": 0}      // mm/s, mm/s, mrad/s
{"cmd": "stop"}                    // close the gate now (CTRL = 0)
{"cmd": "clear"}                   // clear sticky faults, gate stays closed
```

Status datagram:

```json
{"t":123.4,"seq":57,"armed":true,"enabled":true,"gate_open":true,
 "fault_timeout":false,"fault_estop":false,"out":[0.1,0.0,0.0],"cmd":[100,0,0],
 "loop_ms":{"p50":20.04,"p99":20.43,"max":20.54},"overruns":0,
 "cmds":50,"deadman_trips":1,"frames":630,"timeouts":0}
```

`out` is what the **gate** released (what we put on the wire), `cmd` is what was
requested — they differ exactly when the gate is clamping, which is the whole
point of the design.

## 3. Safety layers (independent, outermost last)

| Layer | Trigger | Reaction | Deadline |
|---|---|---|---|
| dog firmware | no velocity frame | stops | 250 ms |
| PL safety gate | no arm write | OUT_* → 0, sticky fault | 200 ms |
| this service (deadman) | no command | CTRL=0, sends zeros | 300 ms |
| SIGTERM / SIGINT | service stop, reboot | CTRL=0 before exit | immediate |
| power-on default | gate starts closed | nothing moves | — |

The service deadman is deliberately the *slowest* layer: the hardware must win.

## 4. Files

| Local | Board |
|---|---|
| `host/app/pl_service.py` | `/home/xilinx/safety_gate/pl_service.py` |
| `host/app/gate_bridge.py` | `/home/xilinx/safety_gate/gate_bridge.py` |
| `host/deploy/fpga-gate-bridge.service` | `/home/xilinx/safety_gate/fpga-gate-bridge.service` |
| `host/deploy/install_service.sh` | `/home/xilinx/safety_gate/install_service.sh` |
| `tools/pl_cmd.py`, `tools/pl_sim_demo.py` | (notebook only) |
| — | `/etc/systemd/system/fpga-gate-bridge.service` (the only file outside the dir) |

## 5. Install (board)

```bash
# 1. push the five files into /home/xilinx/safety_gate/ (remote.py --push)
# 2. smoke test in the foreground first (no service yet)
echo xilinx | sudo -S /home/xilinx/safety_gate/run.sh \
    /home/xilinx/safety_gate/pl_service.py --duration 10
#    expect: clock check ratio ~1.000, "PL gate ready", idle zeros on the wire
# 3. install
echo xilinx | sudo -S bash /home/xilinx/safety_gate/install_service.sh
# 4. verify from the notebook (sends nothing, only listens)
python tools/pl_cmd.py --watch 5
# 5. verify restart-on-crash
echo xilinx | sudo -S systemctl kill -s SIGKILL fpga-gate-bridge
sleep 3 && python tools/pl_cmd.py --watch 2
# 6. reboot the board once and repeat step 4 without logging in
```

## 6. Verify without the dog

```bash
python tools/pl_sim_demo.py     # fake gate + scripted client, ~5 s, prints PASS
python -m pytest tests/test_pl_service.py -q
python tools/pl_cmd.py --watch 5 --board 192.168.1.101
```

## 7. Verify with the dog (needs a human standing by, conventions §1)

```
plan: 0.10 m/s forward for 2 s through the service, dog standing, remote in hand
expect: gate opens within one period, out=(0.10,0,0), dog walks ~0.17 m
abort: python tools/pl_cmd.py --stop   (or Ctrl-C, which sends stop)
```

## 7b. Verified on hardware (2026-10-26)

| Check | Result |
|---|---|
| overlay load | 0.85 s |
| clock self-check | 27.8 MHz, ratio 1.000 |
| idle frames | 1200 over 8 s = 150/s (3 × 50 Hz) |
| loop p50 / p99 / max | 20.0 / 20.0–20.1 / — ms, overruns 0 |
| gate at start | closed (status 0x0c), no motion possible |
| restart after SIGKILL | active again in ~2 s, timing unchanged |

## 8. Metrics to report

| Metric | Where it comes from |
|---|---|
| loop period p50 / p99 / max | `loop_ms` in the status packet (sliding 500 periods) |
| deadline misses | `overruns` (period > 1.5 × 20 ms) |
| watchdog trips | PL `STATS[15:0]`, reported as `timeouts` |
| deadman trips | `deadman_trips` (software layer) |
| command→wire latency | to be added in-loop (`t_send - t_cmd`) once on hardware |

## 9. Failure modes

| Symptom | Cause | Action |
|---|---|---|
| `no status packets` | service down / wrong IP | `systemctl status fpga-gate-bridge`, check logs |
| `clock check ... deviates` | wrong bitstream or FCLK not the expected 27.778 MHz | reflash bitstream; do not bypass |
| `sendto failed` | dog board off / wrong dest | check `ping 192.168.1.120` |
| gate keeps re-closing every 300 ms | client not refreshing at ≥5 Hz | check client loop rate |
| `ffmpeg`/overlay busy | `gate_test.py` running while the service owns the PL | stop the service first |

## 10. Rollback

```bash
echo xilinx | sudo -S bash /home/xilinx/safety_gate/install_service.sh --remove
```

Leaves the board with no service and no unit file; `gate_bridge.py` CLI still
works as before. Nothing else on the board is modified.
