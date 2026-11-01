# 常用命令速查（照着复制就行）

> 面向操作：**我要让狗做什么，在哪儿敲什么**。
> 详细原理在 `docs/runbook_*.md`，本文只放能直接粘贴的命令。
> 所有命令都在仓库根目录 `E:\zongy\WORK\Claude code\FPGAandDOG` 下执行。
> 最后更新：2026-10-27

---

## 0. 先看这一页（最常用的 6 条）

| 我想…… | 在 PowerShell 里敲 |
|---|---|
| 让狗开始听人说话（**会走**） | `python tools/voice_agent.py --allow-motion` |
| 让狗开始听人说话（**只读不动**，最安全） | `python tools/voice_agent.py` |
| 不发语音，直接让它读眼前数字 | `python tools/digit_live.py --shots 5` |
| 看狗的状态（姿态/电量/速度） | `python host/app/state_monitor.py --port 43900` |
| 看 FPGA 闸门状态 | `python tools/pl_cmd.py --watch 3` |
| 跑一遍全部自检 | `python -m pytest tests/ -q` |

**PowerShell 先设一次编码**（否则中文日志可能乱码）：

```powershell
$env:PYTHONIOENCODING="utf-8"; chcp 65001 > $null
```

> `MSYS_NO_PATHCONV=1` 只在 **Git Bash** 里需要；PowerShell 不用加。

---

## 1. 启动前检查（30 秒）

```powershell
# 1) 三块板子都在线
ping 192.168.1.100 -n 1   # ZYNQ / FPGA 闸门
ping 192.168.1.120 -n 1   # 机械狗
ping 192.168.1.103 -n 1   # 感知板（相机）

# 2) 状态流有没有在跑（要看到 basic=6 站立、age 很小、battery 够）
python host/app/state_monitor.py --port 43900

# 3) FPGA 闸门的守护进程在不在
python tools/pl_cmd.py --watch 3
#   期望：gate=closed armed=n out=(0.00,0.00,0.00) p50≈20ms over=0 trips=0
```

三条都正常再往下。任何一条不对，看第 9 节。

---

## 2. 让小狗开始监听（语音主程序）

### 2.0 最省事：双击桌面图标（推荐）

桌面上已经放好两个快捷方式，**双击即用**，会自动 cd 到仓库目录、设好 UTF-8、记日志：

| 图标 | 等价命令 | 用途 |
|---|---|---|
| **小狗-听我说话** | `python tools/voice_agent.py --allow-motion` | 真机演示（会走近卡片） |
| **小狗-只读不动** | `python tools/voice_agent.py` | 彩排/排查（只听 + 读数字，绝不移动） |

- 窗口里会先显示横幅和日志路径，"会走近卡片"那个还会等 2 秒让你确认场地清空。
- 跑完（说"小狗，下班"或 Ctrl+C）窗口不会立刻消失，按回车才关，方便你看退出码。
- 日志自动写到 `logs/voice_agent_<时间戳>.log`。
- 想换图案：右键快捷方式 → 属性 → 更改图标（脚本本身是 `tools/run_voice_agent.ps1`）。
- 想改默认行为：直接改快捷方式"目标"里的参数，或改脚本顶部 `param` 块。

### 2.1 手动敲命令

```powershell
# 基础版：只听 + 读数字，不会走（演示最安全）
python tools/voice_agent.py

# 完整版：允许它走近卡片（真机演示用这个）
python tools/voice_agent.py --allow-motion

# 存日志（推荐，出问题能查）
python tools/voice_agent.py --allow-motion --log logs/voice_agent_20261028.log

# 不想让它出声（调试时只看日志）
python tools/voice_agent.py --allow-motion --no-tts

# 只跑 60 秒自动退出（测链路用）
python tools/voice_agent.py --allow-motion --duration 60
```

起来后会听到 **“我在听”**。然后说：

| 你说 | 它做什么 |
|---|---|
| 小狗，看看是什么数字 | 走近到卡片 120 px → 停稳 1.5 s → 读 → 播报 |
| 小狗，去一号位置看看数字 | 直走 0.6 m → 读 → 播报 |
| 停下 / 别动 | 立刻停车（不会退出监听） |
| 小狗，下班 | 播报后退出 |

> 想让它停：**说“停下”，或者直接碰手柄**（手柄会抢走控制权）。

---

## 3. 不发语音，直接下一条命令（assistant.py）

```powershell
# 只说话，不走（把指令当文本喂进去）
python host/app/assistant.py --text "去一号位置看看数字" --no-tts

# 真的走（必须带 --approve，规矩见 conventions 第 1 条）
python host/app/assistant.py --text "去一号位置看看数字" --move --approve

# 用麦克风说一句（录 6 秒，说完自动停）
python host/app/assistant.py --mic 6 --mic-vad --move --approve
```

---

## 4. 单独测某一环（排查用）

```powershell
# 只读数字，不走路；会打印每一帧读数和证据图路径
python tools/digit_live.py --shots 5

# 直着走几趟（0.30 m 一趟，来回 3 次）
python tools/move_batch.py --cycles 3 --forward 0.30

# 转向验收：左 45°、左 45°、右 90°，再走 0.5 m
python tools/turn_check.py --turns 45,45,-90 --point 0.5,0 --rearm 0x21010C03 --approve

# 只看计划、不动（不加 --approve 就只打印）
python tools/turn_check.py --turns 45,45,-90

# 卡片不在正前方时：自己扫视 360° 找到 → 对准 → 走近 → 读（会动狗）
python tools/seek_card.py --approve

# 场地小、只许原地转不许走（推荐；2026-10-28 跑通用这条）
python tools/seek_card.py --want 8 --no-approach --min-clearance 0.40 --approve

# 按数字找：转一圈，只停在读到你指定的那个数字上
python tools/seek_card.py --want 8 --approve

# 扫视阶段的超声净空门槛 / 总行程上限（米）
python tools/seek_card.py --want 8 --no-approach --min-clearance 0.40 --max-travel 0.30 --approve

# 卡片已经在正前方、只想走近读（不扫视）
python tools/seek_card.py --approve --no-sweep

# 只看它打算怎么找（不动狗）
python tools/seek_card.py
```

---

## 5. 控制权 / 急停（会动狗，逐次批准）

```powershell
# 拿控制权 → 切自主模式（狗才肯听我们发的速度）
python tools/dog_simple.py --code 0x21010C03 --value 0 --frames 1 --approve --approve-mode

# 还权给手柄 → 切手动模式（测试结束一定做这一步）
python tools/dog_simple.py --code 0x21010C02 --value 0 --frames 1 --approve --approve-mode

# 软急停
python tools/dog_simple.py --code 0x21020C0E --value 0 --frames 1 --approve --approve-mode
```

> 规律：**狗每次停下之后会丢掉我们的速度流**，下一条运动指令前要重发一次 `0x21010C03`
> （脚本里已经自动做了，手敲命令时要注意）。

---

## 6. FPGA 闸门（ZYNQ，192.168.1.100）

```powershell
# 换 bitstream：推上去 + 重启托管服务
python tools/remote.py --push pynq fpga/build/safety_gate/safety_gate.runs/impl_1/system_wrapper.bit /home/xilinx/safety_gate/system.bit
python tools/remote.py --push pynq fpga/build/safety_gate/safety_gate.gen/sources_1/bd/system/hw_handoff/system.hwh /home/xilinx/safety_gate/system.hwh
python tools/remote.py pynq "echo xilinx | sudo -S systemctl restart fpga-gate-bridge"

# 验收闸门（27 项，期望 ALL CHECKS PASSED）
python tools/remote.py pynq "echo xilinx | sudo -S /home/xilinx/safety_gate/run.sh /home/xilinx/safety_gate/gate_test.py"

# 只看闸门状态
python tools/pl_cmd.py --watch 3
```

> 板上 `system.bit` 和 `system.hwh` **必须同名同目录**，否则 overlay 加载失败。
> 换完 bit 一定要重启 `fpga-gate-bridge`（它启动时只加载一次）。

---

## 7. 感知板（103）与狗（120）

```powershell
# 103 的状态流转发（板子重启后不会自启，必须手动来一下）
python tools/remote.py perception "bash /home/ysc/robot_state_relay/start_state_tee.sh"

# 103 的抓图服务（读卡前必须有；掉了就重启）
python tools/remote.py perception "cd /home/ysc/digit && setsid nohup python3 -u capture_server.py --device 2 --port 43920 >> capture_server.log 2>&1 < /dev/null &"

# 想钉死曝光（可选）：--exposure 是 100us 单位，0=自动；--gain 省略就交给相机
# 当前室内自动曝光约 100（10ms）；低于 80 会过不了 min_brightness=160
python tools/remote.py perception "cd /home/ysc/digit && setsid nohup python3 -u capture_server.py --device 2 --port 43920 --exposure 100 --gain 64 >> capture_server.log 2>&1 < /dev/null &"
# 注意：流开着时用 v4l2-ctl 改控制会把服务弄崩（2026-10-28 实测），要改就重启服务

# 上板子看看（密码在 tools/hosts.local.json 里）
python tools/remote.py pynq "uname -a"
python tools/remote.py dog "uptime"
python tools/remote.py perception "ls /home/ysc"
```

---

## 8. 自检

```powershell
python -m pytest tests/ -q          # 全部（当前 299 passed）
python -m pytest tests/test_voice_commands_doc.py -q   # 语音词表/播报是否和文档同步
python -m pytest tests/test_motion_exec.py -q          # 运动闭环
```

---

## 8.1 重训数字模型（离线，不需要狗）

模型坏了、或者现场样本攒够了想再调，就按这两条重跑（都在仓库根目录，PowerShell 也可）：

```powershell
# 1) 生成"合成手写"数据集（200 个字体、5 万张，约 3 分钟）
python tools/render_digits.py --per-digit 5000 --fonts 200 --out .cache/synth_digits.npz

# 2) 训练备模型（MNIST + 合成，约 1 分钟），产物顺带被级联自动接上
python tools/train_digits_v2.py --synth .cache/synth_digits.npz --epochs 10 --hidden 256 `
       --out host/model/digits_mlp_v2.npz
```

- `host/model/digits_mlp.npz` 是**主模型**（MNIST），`digits_mlp_v2.npz` 是**备模型**；
  两个文件同时在场时 `load_classifier()` 自动组成级联（主模型没把握才问备模型）。
- 想退回纯 MNIST：把 `host/model/digits_mlp_v2.npz` 改名或删掉即可，无需改代码。
- 验收三件套（改完必跑）：

```powershell
python tools/bench_scenes.py                      # 期望 195/200 左右
python tools/bench_stress.py --reader plain       # 期望 44/52 左右
python _scratch/eval_real.py                      # 8 张真机标注帧，期望 7/8
```

## 9. 出问题先看这里

| 现象 | 多半是 | 怎么办 |
|---|---|---|
| 说话没反应，日志 `no speech above rms` | 麦克风没收音 | 换 `--device N`；或看 Windows 输入设备 |
| 语音听懂了，狗不动，日志 `dog is not responding` | ①手柄抢了控制权 ②停下后没重发模式码 | 手离开手柄；重发 `0x21010C03`（脚本已自动） |
| `暂时不能移动：...` | 没站立（要 `basic=6`）/ 电量低 / 没开 `--allow-motion` | 让它站起来、换电池 |
| `read: 相机没连上` | 103 上 `capture_server` 挂了 | 第 7 节重启抓图服务 |
| `state_monitor` 一直没输出 | 103 上状态转发没跑 | 第 7 节 `start_state_tee.sh` |
| `43900 已被占用` | 另一个程序在看状态 | 关掉那个终端（同一时刻只能一个） |
| 读数字读成别的数 | 笔太淡 / 卡片太小 / 背景太像 | 换粗黑马克笔、换棕色背景、走近（120 px） |
| 闸门 `trips` 变大 | 看门狗超时触发过 | 正常保护；查是不是命令流断了 |
| 上板读不到 overlay | bit 和 hwh 名字/目录不一致 | 保证同名同目录再重启服务 |

---

## 10. 常用参数速记

| 参数 | 作用 |
|---|---|
| `--allow-motion` | 允许真实运动（不加就只读不走） |
| `--approve` | 确认动作预案已批准（不加只打印计划） |
| `--approve-mode` | 控制权/姿态/急停码的额外确认 |
| `--shots N` | 读卡连拍几帧（默认 3，现场可 5） |
| `--settle S` | 停稳后等几秒再读（默认 1.5） |
| `--approach-px 120` | 卡片宽到这个像素就停（走近阈值） |
| `--rearm 0x21010C03` | 每个动作前重发模式码 |
