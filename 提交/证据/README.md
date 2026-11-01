# 证据包：每条结论对应哪个文件

本目录里放的**全部是程序自己吐出来的原始输出**（日志、CSV、自检输出、测试结果），
不是事后补写的描述。每条结论都能对应到一个文件，文件里的数字都能用旁边给出的命令重跑。

---

## 一、总表

| # | 结论 | 证据文件 | 复现命令 |
|---|---|---|---|
| 1 | ZYNQ 安全闸门 27 项自检全过（含看门狗 201.1 ms、斜坡 20 mm/s/ms） | `fpga/gate_test_20261101.txt` | 板上 `sudo systemctl stop fpga-gate-bridge && run.sh gate_test.py` |
| 2 | 每段运动都经过闸门：一次演示里 **42 次开门 / 42 次关门**，无例外 | `fpga/pl_service_run_20261101.log` | 板上 `grep -c 'gate enabled' pl_service.log` |
| 3 | 上位机失联时闸门自己关：硬件闩锁计数 **1 → 2 → 3** | `fpga/deadman_20261101.log` + `fpga/gate_evidence_20261101.md` | `python _scratch/deadman_test.py`（发 0.3 s 命令后不发停止帧） |
| 4 | 转向精度：45°/45°/−90° 三次误差 **−1.1° / −0.9° / +1.6°**，直行 0.504 m（横向偏差 0.006 m） | `motion/turn_check_20261027T204625.csv` | `python tools/turn_check.py --turns 45,45,-90 --point 0.5,0 --rearm 0x21010C03 --approve` |
| 5 | 数字读数：同一张卡连续 10 次 **10/10 正确**（置信度 0.77–0.81） | `vision/read10_20261028.csv` / `_b.csv` / `_c.csv` | 见 `docs/progress.md` 2026-10-28 节 |
| 6 | 端到端语音链路：听到"小狗去八号"→ 转圈找到 8（置信度 **0.9937**）→ 走近到 174 px → 读出 **8 @0.991** → 播报"板子上写的是八" | `voice/voice_agent_hunt8_20261101_152539.log` | `python tools/voice_agent.py --allow-motion` 然后说那句话 |
| 7 | 314 项单元测试全过 | `tests/pytest_20261101.txt` | `python -m pytest tests/ -q` |

---

## 二、几个关键文件的读法

### `fpga/pl_service_run_20261101.log`

一段真实演示（板端时钟 17:15–17:30）里闸门的状态切换，共 84 行：

```
[INFO]  gate enabled by fresh command     ← 收到新的非零速度帧，开门放行
[WARNING] gate closed: stop command       ← 收到停止帧，关门归零
```

**42 开 / 42 关，一一配对**。这说明不存在"绕过闸门直接发给狗"的路径——
每一段运动都必须先有新的命令帧开门。

### `fpga/deadman_20261101.log`

```
[2026-11-01 16:04:45,004] [INFO]    gate enabled by fresh command
[2026-11-01 16:04:45,583] [WARNING] gate closed: deadman: no command for 300 ms
```

配合寄存器回读：`trips` 在两次失联后 1 → 2 → 3（粘性，只增不减）。
这就是"上位机崩溃也停得住"的硬证据：连停止帧都没发出来，闸门 300 ms 内自己关了，
硬件看门狗 200 ms 内把输出归零。

### `voice/voice_agent_hunt8_20261101_152539.log`

```
15:29:05 HEARD: 小狗去八号
15:29:05 INTENT: {'intent':'goto','target':8,...}
15:29:21 hunt 8 result: found=True, seen=[(0.0,'3',0.759),(-80.0,'8',0.9937)]
15:29:21 SPEAK: 找到了，数字八在这边
15:29:21 approach: card width 174 px (target 120) → close enough
15:29:23 read result: {"digits":"8","confidence":0.991,"agreement":1.0,"shots":3,"reason":"ok"}
15:29:24 SPEAK: 板子上写的是八
```

一句话跑完全链路：转圈扫视（9 个停点）→ 在 −80° 停点认出 8 → 判断距离 → 走近 →
停稳连拍 → 读数 → 播报。**同一次运行里的 `seen` 还记录了 0° 停点把另一张卡读成 3**，
说明它真的在不同方向逐张识别，而不是碰巧对着同一张卡。

---

## 三、没有视频，所以就没有的东西

诚实说明：**没有录制演示视频**，因此以下内容本证据包不能证明：

- 狗连续动作的观感（转向是否平滑、走近是否稳）——只有 `motion/` 里的数值；
- 现场光照/场地条件下的稳定性——只有 `vision/` 里那次 10/10 的读数记录；
- 答辩所需的三分钟呈现。

已在 `docs/progress.md` 与 `docs/runbook_*.md` 里保留了所有实测数字的上下文
（测试条件、卡面字号、距离、电量），便于评委按同一条件复跑。
