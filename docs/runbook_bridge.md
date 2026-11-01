# 命令通路：PL 安全闸门 → Lite3 UDP 帧

> 目标：让 FPGA 真正站在"决策"和"狗"之间。决策层写 `CMD_*` 并喂狗，PL 决定实际放行多少速度，
> 桥接进程只做搬运（读 `OUT_*` → 组帧 → UDP），绝不自己发明速度。

## 1. 数据流

```
决策层 (语音/导航/手动)
   │ 写 CMD_VX/VY/WZ + CMD_ARM (50 Hz)
   ▼
PL safety_gate   ← 硬件看门狗，喂狗停 >200 ms 就把 OUT_* 归零并锁存 fault
   │ OUT_VX/VY/WZ (已放行的值)
   ▼
gate_bridge (PYNQ PS 或 PC)         ← 本次新增，纯搬运
   │ Lite3 帧：0x0140/0x0145/0x0141 (double 小端) + 0x21040001 心跳
   ▼
UDP → 狗 120:43893 / 或测试 sink
```

FPGA 职责不变：**超时归零、急停闩锁、计数与心跳**。网络与协议编码仍在上位机，
因为这块板子的以太网 PHY 接在 PS 的 MIO 上，PL 没有物理出口——这条边界要在报告里写清楚。

## 2. 文件

| 文件 | 作用 |
|---|---|
| `host/app/gate_bridge.py` | 桥接器；`--source pynq` 读真 PL，`--source sim` 软件替身；`--dry-run` 只打印帧 |
| `host/app/udp_sink.py` | 收帧并解码，用来在**没有狗**的情况下验证通路的字节与速率 |

## 3. 用法

```bash
# 1) 纯软件：打印会发到线上的字节
python -m host.app.gate_bridge --dry-run --command 100,-50,300 --seconds 0.06

# 2) 软件替身 → 本机 sink（不需要板子和狗）
python -m host.app.udp_sink --listen 43901 --seconds 10 &
python -m host.app.gate_bridge --source sim --dest 127.0.0.1:43901 --command 100,-50,300 --seconds 2

# 3) 板端真 PL → 板端 sink（本次验证用的就是这个）
python tools/remote.py pynq "cd /home/xilinx/safety_gate && \
  (nohup /usr/bin/python3 udp_sink.py --listen 43901 --seconds 10 > /tmp/sink.log 2>&1 &) ; sleep 1; \
  echo xilinx | sudo -S /home/xilinx/safety_gate/run.sh /home/xilinx/safety_gate/gate_bridge.py \
    --source pynq --dest 127.0.0.1:43901 --command=-250,-40,0 --seconds 2; sleep 6; cat /tmp/sink.log"

# 4) 发给狗（必须带 --approve，且先交动作预案，见 conventions.md §1）
python -m host.app.gate_bridge --source pynq --dest 192.168.1.120:43893 --command 100,0,0 --seconds 3 --approve
```

注意 `--command` 取负数时要写成 `--command=-250,-40,0`，否则 argparse 会把它当选项名。

## 4. 验证记录（2026-10-25，真实 PL）

| 用例 | 命令 | sink 观测 |
|---|---|---|
| 正速度 | `--command 100,-50,300` | 0x0140=0.1000 / 0x0145=4294967.2 ❌ / 0x0141=0.3000 |
| 负速度（修符号后） | `--command=-250,-40,0` | 0x0140=-0.2500 / 0x0145=-0.0400 / 0x0141=0.0000 |
| 不喂狗（`--no-arm`） | 同负速度 | 三路全 0.0000 ✅ 闸门没开，帧里就是 0 |

三次都是 **50.0 帧/s × 3 路 + 2 次/s 心跳**，`0 malformed`。

### 4.1 修掉的真实 bug
第一版 `PlGate.outputs()` 直接把 MMIO 读出的 `OUT_VY` 当无符号数除 1000：
`-50 mm/s` 读成 `0xFFFFFFCE` → `4294967.246 m/s`。
现在 `_to_int32()` 先按 32 位有符号重解释再换算。
**这个 bug 只有真板子能暴露**——软件替身里是 Python 负数，不会出现。

### 4.2 顺手修的两处安全问题
1. 上一轮跑完留下粘性 `fault_timeout`，下一轮启动时闸门打不开。现在 `PlGate` 初始化先写
   `CTRL = ENABLE | CLEAR_FAULTS` 再 `ENABLE`，显式清一次。
2. 停止时的 0.5 s 零速保持现在**继续喂狗**，避免这段空窗自己触发故障、把状态弄脏；
   进程一退出喂狗自然停，狗侧还有自己的 250 ms 命令超时兜底。

## 5. 设计约束（写报告时别写错）

- 桥接器**不产生速度**：`--source pynq` 时三路值 100% 来自 `OUT_*`；闸门关就发 0。
- `--no-arm` = 纯搬运模式：喂狗属于决策层，桥接器不越权。
- PL 无以太网出口（PHY 在 PS MIO），所以 PL 不能自己发包；这是硬件事实，不是实现偷懒。

## 6. 下一步（等狗）

1. 狗充满电、用户在场 → 把 `--dest` 指向 `192.168.1.120:43893`，先 `--command 0,0,0` 观察狗是否接受该源；
2. 接受后做 ≤0.1 m/s × 1 s 的位移测试，用状态流 `vel_body/pos` 判定是否真的动；
3. 若狗只认感知板（103）下发的速度，则在 103 上跑同一个桥接器（PL 的 `OUT_*` 需经 101→103 转发）。
