# FPGA 安全闸门 overlay 运行手册（P0-1）

> 目标：让 ZYNQ-7020（192.168.1.101）能加载我们自己的 PL 设计，并在 PYNQ 里用 `MMIO`
> 验证「命令超时 → 速度归零」这条安全性质。本阶段**不接狗**，纯 PL 自检。

## 1. 这块逻辑是什么

`safety_gate` 是一个 AXI4-Lite 从设备（基地址 `0x43C00000`，64 KB 窗口），把「主机下发的速度命令」
和「真正送给狗的速度」隔开：

- 主机写命令 + 写 `CMD_ARM` 才「上膛」（重新计时）
- 主机必须在 `TIMEOUT_MS`（默认 200 ms）内**反复**写 `CMD_ARM`，否则闸门自动关闭、
  输出速度归零，并置位粘滞故障位
- 软件 e-stop（`CTRL.bit1`）或外部 e-stop 引脚（`estop_in`）会立刻切断输出
- `STATUS` 与 `STATS` 记录状态与时间戳/计数，供事后举证

### 寄存器映射

| 偏移 | 名称 | 读写 | 含义 |
|---|---|---|---|
| 0x00 | VER | RO | `0x0001_0000` |
| 0x04 | CTRL | RW | bit0 enable，bit1 estop，bit2 clear_faults |
| 0x08 | TIMEOUT_MS | RW | 看门狗超时，默认 200 ms |
| 0x0C | CMD_VX | RW | 期望前进速度，有符号，mm/s |
| 0x10 | CMD_VY | RW | 期望侧向速度，有符号，mm/s |
| 0x14 | CMD_WZ | RW | 期望偏航角速度，有符号，mrad/s |
| 0x18 | CMD_ARM | RW | 写任意值 = 上膛 + 重新计时 |
| 0x1C | OUT_VX | RO | 闸门后的前进速度，mm/s |
| 0x20 | OUT_VY | RO | 闸门后的侧向速度，mm/s |
| 0x24 | OUT_WZ | RO | 闸门后的偏航角速度，mrad/s |
| 0x28 | STATUS | RO | bit0 gate_open，bit1 wd_armed，bit2 !estop，bit3 enable，bit4 fault_timeout（粘滞），bit5 fault_estop（粘滞） |
| 0x2C | STATS | RO | [15:0] 超时次数，[31:16] 接受的 arm 次数 |
| 0x30 | HEARTBEAT | RO | 自由计数，用来反推 PL 实际时钟 |
| 0x34 | RELOADS | RO | 看门狗被成功重装的次数（诊断用，等一次 arm 加一） |

### 时钟（重要，踩过坑）

PL 时钟不是 50 MHz。实测：

```
heartbeat +139032028 in 5.0052 s -> 27.778 MHz
```

原因：这块板子的 IO PLL 实际跑 **666.667 MHz**，而 PYNQ 的 `Clocks` 按 1000 MHz 算分频，
所以 PYNQ 说 `fclk0 = 41.666667 MHz` 时，PL 真正拿到的是 27.778 MHz（= 666.667/24）。
验证过：`Clocks.fclk0_mhz = 50` 后实测变 33.333 MHz（= 666.667/20），正好 2/3 关系。

因此 RTL 的 `CLK_HZ` 参数取 **27_777_778**（`fpga/src/safety_gate.v` 头部有完整说明）。
自检脚本会在开头实测心跳速率并与该常数比对（容差 ±5%），**时间基准不对就不许开闸** ——
否则 200 ms 的超时会被悄悄拉长到 360 ms。

## 2. 怎么构建

分两段（第一段建 BD 并返回，第二段跑综合/实现）：

```tcl
# Vivado 2026.1 Tcl 控制台
source "E:/zongy/WORK/Claude code/FPGAandDOG/fpga/build/build.tcl"   ;# 建工程 + BD + wrapper
launch_runs synth_1 -jobs 8 ; wait_on_run synth_1                     ;# ~2 min
launch_runs impl_1 -to_step write_bitstream -jobs 8 ; wait_on_run impl_1 ;# ~2 min
```

- 源码：`fpga/src/safety_gate.v`
- 构建脚本：`fpga/build/build.tcl`（PS7 配置直接复用正点原子 2_axi_gpio 示例的
  `CONFIG.PCW_*`，保证与板子上跑着的 PYNQ 镜像兼容；该配置片段在
  `_scratch/vendor_bd/ps7_config.tcl`）
- 产物：
  - 比特流 `fpga/build/safety_gate/safety_gate.runs/impl_1/system_wrapper.bit`
  - 硬件描述 `fpga/build/safety_gate/safety_gate.gen/sources_1/bd/system/hw_handoff/system.hwh`

## 3. 怎么部署到板子

板端目录固定为 `/home/xilinx/safety_gate/`，其中 `system.bit` 与 `system.hwh`
**必须同名同目录**（PYNQ 靠它把寄存器地址对上）。

```bash
# PC 侧（Git Bash 要加 MSYS_NO_PATHCONV=1，否则远端路径会被改成 Windows 路径）
BIT=fpga/build/safety_gate/safety_gate.runs/impl_1/system_wrapper.bit
HWH=fpga/build/safety_gate/safety_gate.gen/sources_1/bd/system/hw_handoff/system.hwh
MSYS_NO_PATHCONV=1 python tools/remote.py --push pynq $BIT /home/xilinx/safety_gate/system.bit
MSYS_NO_PATHCONV=1 python tools/remote.py --push pynq $HWH /home/xilinx/safety_gate/system.hwh
MSYS_NO_PATHCONV=1 python tools/remote.py --push pynq host/app/gate_test.py /home/xilinx/safety_gate/gate_test.py
```

## 4. 怎么验证

板端有统一入口 `/home/xilinx/safety_gate/run.sh`（PYNQ venv + `XILINX_XRT` 都在里面配好）：

```bash
# PC 侧一条命令（root 是 PYNQ 访问 /dev/mem 需要的）
MSYS_NO_PATHCONV=1 python tools/remote.py pynq \
  "echo xilinx | sudo -S /home/xilinx/safety_gate/run.sh /home/xilinx/safety_gate/gate_test.py"
```

注意：**不要**在远端命令里直接写 `$PATH`/`$XILINX_XRT`，本地 Git Bash 会先把它们展开成
Windows 的路径（带括号），远端报 `syntax error near unexpected token '('`。环境一律封在 run.sh 里。

最近一次结果（2026-10-27 晚：新比特流 + 斜坡修复，**27 项全过**）：

```
overlay loaded in 0.55s
[PASS] version register 0x10000
[PASS] clock matches RTL constant 27.777 MHz (expected 27.778, ratio 1.000)
[PASS] gate closed while disabled 0x4
[PASS] output zero while disabled
[PASS] gate open after arm 0xf
[PASS] wd ok bit 0xf
[PASS] command not passed through instantly first read 0
[PASS] out_vx follows command after ramp 100
[PASS] out_vy follows command after ramp -50
[PASS] out_wz follows command after ramp 300
[PASS] ramp reached 500 mm/s 500
[PASS] ramp step bounded by DELTA max 40 mm/s over a 1.2 ms sample gap (allowed 40)
[PASS] ramp step counter matches travel 20 steps for 400 mm/s, expected 20
[PASS] no timeout while feeding 0xf
[PASS] output still following 100
[PASS] every arm reloaded the watchdog reloads +20 feeds 20
[PASS] timeout fault after arming stops after 201.5 ms from the last arm (50.3 ms after the poll started)
[PASS] timeout close to programmed value 201.5 ms vs 200 ms
[PASS] output zero after timeout 0
[PASS] timeout counter incremented timeouts=1 cmds=21
[PASS] command counter incremented timeouts=1 cmds=21
[PASS] fault stays until cleared 0x1e
[PASS] output stays zero while faulted
[PASS] gate closed while faulted 0x1e
[PASS] output returns after clear (ramped) -25
[PASS] output zero on estop 0
[PASS] estopped status 0xa
ALL CHECKS PASSED
```

斜坡那条要看**两行**：主机采样间隔约 1.2 ms，会一次跨过 1 ms 的两个台阶（所以上界按实测
间隔放宽到 40）；真正证明"没有一步跳到位"的是 `ramp step counter matches travel`
—— 硬件步数计数器 +20 正好对应 400 mm/s ÷ 20 mm/s。

四类断言最关键：

1. `version register` = 0x10000（总线通、地址对）
2. `clock matches RTL constant`（时间基准正确，否则超时长度不可信）
3. `every arm reloaded` + `no timeout while feeding` + `timeout ... 200.2 ms` + `output zero after timeout`
   （**核心安全性质**：停止喂心跳后 200 ms 内输出归零）
4. `output zero on estop`（急停优先级最高）

## 5. 已知限制（本阶段故意留的）

- `estop_in` 未接物理引脚，BD 里被 tie 到 0；真正的急停按钮/引脚留到接狗那一版。
- `OUT_VX/VY/WZ` 目前只是内部寄存器，**没有接到任何引脚或 UDP 发送器**；
  下一版才把闸门输出接到命令发送通路。
- 未加 LED/按键等物理指示灯（板子没接显示器，指示灯意义不大）。
- 未做时序约束文件（当前设计只有 50 MHz 同步逻辑，Vivado 默认时序仍会检查；
  后续加外部 IO 时再补 `.xdc` 与引脚表）。

## 6. 回滚

板子重启即回到 PYNQ 默认状态（overlay 只在内存里）。若要主动卸载：
`sudo -E python3 -c "from pynq import Overlay; Overlay('/home/xilinx/pynq/overlays/base/base.bit')"`
或直接重启板子。

## 7. 排错记录

- `add_files` 遇到路径含空格会把路径拆开 → 用 `[list $src]` 包一层。
- `get_property FILE_NAME [get_files *.bd]` 报 "The object 'file' does not have a property 'FILE_NAME'"
  → 直接 `make_wrapper -files [get_files -quiet *system.bd] -top -import`。
- BD 校验会报 `estop_in` 未连接的 CRITICAL WARNING（自动 tie 0）→ 加 `xlconstant`（1 bit，值 0）接上去。

### 7.1 看门狗「重装不生效」的定位过程（已修，留档）

现象：第二轮自检里 `no timeout while feeding` 与 `output still following` 两条 FAIL，
`STATUS=0x1e`（超时粘滞位置位）。每 50 ms 喂一次、超时 200 ms，本不该超时。

排查顺序与结论（每一步都是实测，不是猜）：

1. 先怀疑探针脚本：写了 `STATUS=0x1C`，其实 0x1C 是 `OUT_VX` → 换成 0x28 重测。
2. 实测时钟：`heartbeat +139032028 in 5.0052 s` = 27.778 MHz ≠ 50 MHz，
   所以「配置 200 ms」的真实时长是 360 ms；单次 arm 的实测超时 1.801 s 也正好是 1.8 倍
   → **第一层错误：时间基准常数错了**。
3. 但 360 ms 仍大于 50 ms 的喂狗间隔，超时不该触发。于是写 `gate_probe2.py`：
   `TIMEOUT_MS=1000`，t=0 上膛，t=1.2 s 再上膛一次。若第二次生效，故障应出现在 3.0 s；
   实测仍为 **1.800 s** → 第二次 arm 根本没重装。
4. `gate_probe3.py` 再补一刀：t=1.2 s 先写 `CTRL=0` 卸膛、再使能、再上膛 → 故障出现在 **3.004 s**。
   结论：**只有 `wd_armed` 本来就是 0 时，重装才生效** —— 即在「已上膛」状态下，
   倒数分支的同周期赋值把重装值覆盖了。
5. 修法：把 `wd_armed`/`wd_cycles_left` 的所有写入收敛到**一个 always 块**里，
   用「本周期有 arm 请求」显式优先于倒数；arm 请求由寄存器解码块打成单周期 `arm_req`。
   同时在 `STATS`/`RELOADS` 里加计数，让「每次 arm 是否真的重装」可被自检断言
   （现在自检直接断言 `reloads +20 == feeds 20`）。

教训：**推论正确不等于实现正确**。同一个 reg 被两个分支赋值时，靠"读代码觉得对"没有意义，
必须用能区分两种假设的实验（这里是「第二枪是否延后」和「先卸膛再上膛」）去逼出结论。

## 8. 命令整形器 cmd_shaper（2026-10-26 夜，已接进 safety_gate，仿真 19 项通过）

### 8.1 为什么要有它
主机 50 Hz 发速度，但一次"卡顿/手滑/脚本重连"就能在一个周期内要求从 0 跳到 1 m/s，
狗会蹿一下。现在把**限幅/斜坡做进 PL**：不管主机要求什么，输出每 1 ms 最多变化
`DELTA_V = 20 mm/s`（转向 `DELTA_W = 20 mrad/s`），0→1 m/s 需要 50 ms 斜坡。

### 8.2 接口（`fpga/src/cmd_shaper.v`，纯同步逻辑，不含 AXI）
| 端口 | 含义 |
|---|---|
| `cmd_vx/vy/wz` + `cmd_valid`（单周期脉冲） | 锁存新目标 |
| `out_vx/vy/wz` | 斜坡后的输出（有符号，mm/s、mrad/s） |
| `step_pulse` | 本周期输出有变化 |
| `step_count` | 已执行的斜坡步数（可读回当证据） |
参数：`CLK_HZ`、`STEP_CYCLES`（默认 1 ms）、`DELTA_V`、`DELTA_W`。

### 8.3 测试台与结果
`fpga/sim/tb_cmd_shaper.v` 自检 14 项，`fpga/sim/run_cmd_shaper.sh` 一键跑（xvlog/xelab/xsim，无需板子）：

```
[PASS] first ramp step: 20          <- 100 的目标第一拍只走 20，不是一步到位
[PASS] settled at 100: 100
[PASS] follows new target: 100      <- 斜坡中途改目标能立刻跟上
[PASS] wz settled: 200
[PASS] back to zero vx: 0
[PASS] step_count advanced to 27
SIM PASS (27 steps counted)
```

### 8.4 接进 safety_gate（2026-10-26 深夜完成）

`fpga/src/safety_gate.v` 的 `OUT_*` 不再直接跟随 `CMD_*`，而是先过一个
`cmd_shaper` 实例（`u_shaper`，`DELTA_V = DELTA_W = 20`，`STEP_CYCLES = CLK_HZ/1000`）：

- 模块参数 `RAMP_DELTA_V` / `RAMP_DELTA_W`（单位：mm/s 与 mrad/s，每 1 ms 步进）。
  **必须写成 `parameter integer`，不能写 `parameter signed [17:0]`**：Vivado 的
  block-design「module reference」包装器会把向量型参数按**字符串**下发，`18'sd20`
  到综合端变成 `"20"` 的 ASCII 码 `0x3230 = 12848`，斜坡退化成一步跳到位。
  2026-10-26 的 bitstream 就踩了这个坑——上板 `gate_test.py` 报
  `ramp step bounded by DELTA max 400`（期望 ≤20），而 RTL 仿真 19 项全过。
- `cmd_shaper` 的 `cmd_*` 在 `gate_open=0` 时被喂 0，`cmd_valid` 恒为 1；
  闸门一关，输出仍由原来的 `!gate_open` 分支**立即置零**（看门狗行为不变）；
- 新增只读寄存器 **`0x38 RAMP_STEPS`**（32 位斜坡步数计数，报告里的吞吐证据）；
- `CMD_*` 先饱和到 ±131071 再进 18 位整形器，避免 32→18 位截断。

#### 仿真结果（`fpga/sim/tb_safety_gate.v`，AXI4-Lite BFM + 时序压缩 CLK_HZ=100k）

`bash fpga/sim/run_safety_gate_sim.sh` → **SIM PASS (19 checks)**，关键几条：

```
[PASS] status after reset (estop_n=1) = 4
[PASS] first step after 1 ms is exactly DELTA (20)
[PASS] command 100 was NOT passed through in one step
[PASS] settle took 5 ms (expected 5)      <- 100 mm/s 用 5 个 20 mm/s 台阶到达
[PASS] max step is DELTA = 20
[PASS] ramp in progress at 160 after target change
[PASS] fault_timeout latched = 16; out forced to zero on timeout = 0
[PASS] ramp steps advanced 50 -> 70
[PASS] re-arm ramps from zero (40)        <- 清故障再武装不会跳回旧指令
```

bench 里踩过的两个坑（已修）：`shp_steps` 在寄存器读 mux 之后声明 → Verilog
"used before its declaration"，把整形器整段移到 AXI 通道之前；`OUT` 的第一个台阶
在 1 ms 之后才出现，不能读得太早（原来 2 个时钟周期就读，读到 0 误判）。

## 9. 斜坡对上层的影响（接板子前先记下来）

| 位置 | 影响 | 处理 |
|---|---|---|
| `host/app/gate_bridge.py` | 它每 20 ms 读一次 `OUT_*` 再发给狗；斜坡让输出比指令晚到 `target/20` ms（100 mm/s → 5 ms，150 mm/s → 8 ms） | 不需要改。20 ms 的转发周期本来就比这慢 |
| `host/app/gate_test.py` | 原来断言"写完 CMD 立刻等于 CMD"，现在必须轮询 | 已改为 `wait_out()` + "指令不会被一步透传" + 斜坡步长按时长放宽 + `RAMP_STEPS` 与位移对账，共 27 项 |
| 真机走位 | 起步更柔和，0.6 m 走位会多花几毫秒 | 等板子回来后重跑 0.6 m，记 `travelled_m` 与 `elapsed_s` |

> 待办：板子一回来就 ①刷新 bitstream ②跑 `gate_test.py`（期望 27 项全过）
> ③跑 `digit_walk` / `assistant --move` 0.6 m 走位 ④确认 `RAMP_STEPS` 在真机上也递增。
