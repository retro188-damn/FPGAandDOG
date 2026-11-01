# 面向室内巡检的语音交互智能机器狗

语音下达"小狗，去找数字八"，四足机器狗自主转身搜索、走近目标卡片、停稳后读取卡面手写数字并语音作答。
运动指令必须先穿过 ZYNQ 可编程逻辑中的硬件安全闸门（200 ms 看门狗 + 故障闩锁 + 指令斜坡整形），
即使上位机失效，速度也会在 200 ms 内被硬件归零。

## 一、系统组成

| 角色 | 设备 | 地址（本机现场） | 关键端口 |
|---|---|---|---|
| 语音与任务编排 | 笔记本 PC | 192.168.1.102 | — |
| 硬件安全闸门 | ZYNQ-7020 开发板 | 192.168.1.100 | 43902（速度命令）/ 43903（状态） |
| 运动执行 | 四足机器人本体 | 192.168.1.120 | 43893（运动/模式码） |
| 感知采图 | 单板计算机 + 彩色相机 | 192.168.1.103 | 43920（JPEG 抓图） |
| 状态回传 | 三块板组成的链路 | PC 监听 43900 | 100 Hz 状态帧 |

## 二、目录结构

```
host/app/          主机侧应用：语音代理、意图、视觉、运动执行器、PL 服务、状态监控
host/model/        数字识别模型（scikit-learn MLP，.npz）
host/deploy/       板端部署脚本（状态中转、抓图服务）
tools/             命令行工具：语音代理、读卡、扫视找卡、转向验收、FPGA 自检、打包等
tests/             单元测试（pytest）
fpga/src/          Verilog：安全闸门、指令斜坡整形
fpga/sim/          仿真测试台与运行脚本
fpga/build/        综合实现脚本（Vivado Tcl）
fpga/deliverable/  已构建的可编程逻辑镜像 system.bit 与硬件描述 system.hwh
docs/              架构、约定、运行手册、进度与经验记录
metrics/           测试证据（现场照片、基准数据）
证据/              本次提交的原始证据：日志、CSV、自检输出、测试结果（见 证据/README.md）
```

## 三、运行环境

- Python 3.9+，依赖见 `requirements.txt`（numpy / scipy / scikit-learn / pillow / paramiko）。
- 无需深度学习框架：视觉链路全部在 CPU 上运行。
- Vivado 2026.1 用于重建可编程逻辑（仅在需要修改 RTL 时使用）。

```bash
python -m pip install -r requirements.txt
```

## 四、快速开始

1. 现场准备：机器人站立、手柄在手动模式、周边 1 m 清空；确认三块板可达。
2. 复制 `tools/hosts.local.json.example` 为 `tools/hosts.local.json`，填入各板地址与登录凭据
   （该文件含密码，**不随包提供**）。
3. 开机自检：

```bash
python host/app/state_monitor.py --port 43900      # 应持续打印 100 Hz 状态
python tools/pl_cmd.py --watch 3                   # 应打印 gate/armed/out 与周期统计
python host/app/gate_test.py --help                # 安全闸门自检脚本
```

4. 启动语音代理（会运动，需显式授权）：

```bash
python tools/voice_agent.py --allow-motion
```

5. 只读模式（只看不动的联调）：

```bash
python tools/voice_agent.py
```

6. 单独验证某一段：

```bash
python tools/digit_live.py --shots 5                                  # 只测读卡
python tools/turn_check.py --turns 45,45,-90 --point 0.5,0 --approve  # 转向与直行
python tools/seek_card.py --want 8 --no-approach --approve            # 转圈找指定数字
```

## 五、可编程逻辑（ZYNQ）

`fpga/deliverable/system.bit` 为已构建镜像，`system.hwh` 为其硬件描述，二者必须同名同目录。
上板步骤（板端执行）：

```bash
sudo systemctl stop fpga-gate-bridge                      # 换 bit 前先停服务
cp system.bit system.hwh /home/xilinx/safety_gate/
sudo systemctl start fpga-gate-bridge                      # 服务会重新加载 overlay
```

自检（27 项，覆盖版本号、时钟比、看门狗超时、斜坡步数、故障锁存与急停）：

```bash
python host/app/gate_test.py
```

## 六、测试

```bash
python -m pytest tests/ -q      # 314 项
```

基准脚本（需要本地基准数据）：

```bash
python tools/bench_scenes.py    # 标准场景 200 张
python tools/bench_stress.py    # 斜视角/遮挡压力集
python tools/bench_ink.py       # 不同墨色
```

## 七、证据

本项目以**代码 + 可复跑的日志**作为提交证据（未录制演示视频）。`证据/README.md` 给出
"结论 → 复现命令 → 证据文件" 对照表，包含：

- `证据/fpga/gate_test_20261101.txt`：安全闸门 27 项自检全部通过；
- `证据/fpga/pl_service_run_20261101.log`：一次演示中 42 次开门 / 42 次关门，无绕过路径；
- `证据/fpga/deadman_20261101.log`：上位机失联后闸门自行关闭，硬件闩锁计数 1→2→3；
- `证据/motion/turn_check_*.csv`：转向误差 −1.1°/−0.9°/+1.6°，直行 0.504 m；
- `证据/vision/read10_*.csv`：同一张卡连续 10 次读数 10/10；
- `证据/voice/voice_agent_hunt8_*.log`：一句语音 → 转圈找到 8（0.9937）→ 读出 8（0.991）；
- `证据/tests/pytest_20261101.txt`：314 项单元测试通过。

## 八、已知限制

- 只有里程计、没有绝对定位：长时间运行会累积漂移（一轮九停点扫视净漂移约 0.14 m）。
- 卡面数字以粗黑马克笔、卡片宽 110–140 px 为最佳条件；铅笔细笔画仍可能读错。
- 机器人本体为采购平台，未改动机械结构。
