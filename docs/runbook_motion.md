# Runbook — 语音指令到狗移动（执行器）

这层把"去 N 号位置"翻译成前进距离，走 `pl_service → PL 安全闸门 → 狗`。
不带 `--approve` 永远不动狗：只打预览，这是规约第 1 条的软件落地。

## 1. 前置条件（缺一不可）

| 条件 | 怎么确认 |
|---|---|
| 狗站立、手动模式 | 遥控器确认；`basic=6/2` |
| 电量 ≥ 20% | 状态帧 `battery_level` |
| 状态流到笔记本 | `python tools/state_monitor.py --port 43900` 有输出 |
| `pl_service` 在跑 | `python tools/pl_cmd.py --watch 3` 有 status |
| 笔记本 43900 空闲 | 同一端口不能被 `state_monitor` 和 `assistant` 同时占用 |

## 2. 演练（不碰狗）

```bash
python host/app/assistant.py --text "去三号位置看看数字" --move --no-tts
```
预期：先说"收到，前往三号位置"，打印 `MOVE: station 3 -> forward 1.80 m at 0.10 m/s`，
然后 `move not executed: pass --approve ...` 退出。

## 3. 真实执行（需操作员就位）

```bash
# 终端 A：看狗
python host/app/state_monitor.py --port 43900

# 终端 B：语音 + 执行
python host/app/assistant.py --mic 6 --mic-vad --move --approve
```

安全边界（全部在软件里，硬件那层还有 200 ms 看门狗兜底）：
- 状态帧超过 0.35 s 没来 → 中止并停车；
- 超时（距离/速度 × 3 + 2 s）→ 中止；
- 电量跌破 20% / `error_state≠0` → 中止；
- 任何异常路径都在 `finally` 里发 `stop`。

## 4. 要记录的数据

| 指标 | 目标 |
|---|---|
| `travelled_m` vs `target_m` | 偏差 ≤ 5 cm |
| `elapsed_s` | ≈ distance/speed |
| 停车残速 | 0（`pl_cmd --watch` 里 `out=(0,0,0)`） |
| 状态过期中止 | 拔网线应 ≤ 0.4 s 触发 |

## 5. 已知限制

- 位置映射是"1 号位 = 0.6 m"，是占位值；场地定下来后改成实测标定表（`STATION_SPACING_M`）。
- 只支持直行；转弯/侧移没做（接口在 `VelocitySender.send` 里已留 `wz`）。
- 到达判据用世界系直线距离，斜着走也算到达；需要更严就改成沿起始朝向投影。

## 6. 控制权交接流程（2026-10-26 事故后定稿）

自主模式会夺走手柄的控制权，所以每次交接都必须由操作员主导、两步确认：

| 步 | 谁做 | 动作 | 通过标准 |
|---|---|---|---|
| 1 | 操作员 | 把狗站好、确认手柄此刻没被碰 | 回"手柄离手了" |
| 2 | 我 | 报告"将发 0x21010C03 一次，恢复手段是 0x21010C02" | 操作员回"发送" |
| 3 | 我 | 发 1 帧，读状态回显 | `basic=6 err=0`，无异常 |
| 4 | 我 | 发 5 cm 探针（0.05 m/s） | `travelled≈0.05` 才算速度通道通 |
| 5 | 操作员 | 手柄放一边，跑 voice_agent | 狗按语音走 |
| 6 | 任一方 | 出事：按急停或碰一下摇杆（自动回手动） | — |

回退顺序：`0x21010C02` → 手柄验证 → `0x21010202` 起立/趴下 → 软急停后重新起立 → 重启运动服务。
每一档都要先报方案、等操作员同意。

## 7. 原地转向与"点左右"（2026-10-26 夜，代码完成、**未上真机**）

`MotionExecutor` 新增三个入口，全部是**闭环**的（读狗自己的 `rpy[3][2]` 航向，不是定时）：

| 调用 | 作用 | 关键约束 |
|---|---|---|
| `turn(angle_deg)` | 原地转 `angle_deg`（+ 逆时针） | 单次 ≤ `max_turn_deg`（默认 180°）、`max_wz` 0.6 rad/s、误差 ±3° 并稳定 3 个控制周期才结束 |
| `turn_to_heading(heading_deg)` | 转到世界坐标绝对航向 | 已在容差内直接返回 `already there` |
| `move_to_point(dx_m, dy_m)` | 先转到该点在机体系的方位，再直线走 | 距离 ≤ 1.0 m；`dx<0` 需要 `allow_reverse`；返回 `[TurnResult, MoveResult]` |

安全闸和 `move()` 完全一致：未 `allow_motion` 拒绝、状态过期中止、电量低于 20% 中止、
`error_state≠0` 中止、`finally` 必发 stop；另加"转了 0.6 s 还一动不动 → 判定不响应（手动模式）"快速中止。

**这些只在仿真里的假狗验证过**（`tests/test_motion_exec.py` 24 项，含"转到 45°""
"−170° 走近路""不响应中止""move_to_point 先转后走"）。真机验证要等狗开机，验收标准：

1. `turn(90)` 三次，每次实际转过的角度 90°±5°；
2. `move_to_point(0.0, 0.5)` 后狗横向偏移 ≤ 0.1 m（即真的朝左/右走了，不是斜着蹭）；
3. 转向过程中按手柄，转向在 0.5 s 内被接管（看门狗 + 交接流程仍生效）。
