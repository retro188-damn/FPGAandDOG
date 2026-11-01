# 运行手册：狗状态分身（state tee）

## 它解决什么问题

Lite3 的运动主板（192.168.1.120）只把状态流推给**一个**固定地址：
`~/jy_exe/conf/network.toml` 里的 `ip=192.168.1.103, target_port=43897`。
所以 PC、ZYNQ 都看不到狗的真实姿态/速度。状态分身把这一路流在 103 上「复制」成三份：

```
狗运动主板 192.168.1.120:34770
        │  原始 0x09xx 帧，0x0901 稳定 100 Hz，总计约 1710 帧/秒
        ▼
state_relay.py  :43897                      ← jy_exe 的发送目标完全不用改
        ├──> 127.0.0.1:43896  厂商 qnx2ros  →  /leg_odom 等 ROS 话题（99.94 Hz 实测）
        ├──> 192.168.1.89:43900   开发电脑
        └──> 192.168.1.101:43900  ZYNQ-7020（安全闸门的输入，待接入）
```

**为什么不直接改狗**：狗那边改配置要重启 `jy_exe`，有让狗失能的风险；分身方案对狗零改动。

## 文件

| 文件 | 跑在哪 | 作用 |
|---|---|---|
| `host/app/state_relay.py` | 103 | 收 43897，转发三份；每 5 s 打一次各帧类型的计数 |
| `host/app/state_monitor.py` | PC / ZYNQ | 监听 43900，解码 `0x0901` 打印姿态/速度/电量/超声，可落 JSONL |
| `host/deploy/start_state_tee.sh` | 103 | 幂等启动（先停旧的再起） |
| `host/deploy/stop_state_tee.sh` | 103 | 回滚到厂商原始布局 |

## 部署（从 PC 执行）

```bash
cd "E:/zongy/WORK/Claude code/FPGAandDOG"
MSYS_NO_PATHCONV=1 python tools/remote.py --push perception host/deploy/start_state_tee.sh /home/ysc/robot_state_relay/start_state_tee.sh
MSYS_NO_PATHCONV=1 python tools/remote.py perception "chmod +x /home/ysc/robot_state_relay/start_state_tee.sh && bash /home/ysc/robot_state_relay/start_state_tee.sh"
```

> `MSYS_NO_PATHCONV=1` 是必须的：Git Bash 会把参数里的 `/home/...` 改写成 `C:/Program Files/Git/home/...`，导致 SFTP 报 `FileNotFoundError`。

## 验证（从 PC 执行）

```bash
python host/app/state_monitor.py --port 43900
```
正常输出（2 行/秒）：
```
basic=6(stand/idle) rpy=(-0.10,-0.14,2.82) pos=(0.264,0.006,0.313) vel_body=(-0.02,-0.01,-0.01) batt=46% us=(1.09,4.50) err=0 mot=0
```
判据：`0x0901` 达到 100 Hz（脚本的 `--json-log` 里数行数即可）；ROS 侧 `rostopic hz /leg_odom` ≈ 100 Hz。

## 回滚（演示现场救命用）

```bash
MSYS_NO_PATHCONV=1 python tools/remote.py perception "bash /home/ysc/robot_state_relay/stop_state_tee.sh"
```
回滚后 43897 归还给厂商 `qnx2ros`，与出厂状态一致。

## 已知限制

- **103 重启后分身不会自动起来**（尚未做 systemd 单元），需要重跑 `start_state_tee.sh`。
- 分身只转发，不解析；解析在消费者侧（`state_monitor.py` / ZYNQ），改字段不用动板子。
- 原始流量约 1710 帧/秒 ≈ 400 KB/s，接收端必须把 `SO_RCVBUF` 调大（默认 64 KB 会丢 1/3 的帧，实测 67 Hz vs 100 Hz）。

## 帧格式（本项目实测，用于解码）

```c
struct CommandHead { uint32 code; uint32 parameters_size; uint32 type; };  // 12 bytes, little endian
// packet length = 12 + parameters_size; payload doubles are IEEE-754 little endian
```

| code | 长度 | 含义 | 频率 |
|---|---|---|---|
| `0x0901` | 200 B | RobotState：basic、rpy、rpy_vel、acc、pos_world、vel_world、vel_body、error、battery、ultrasound | 100 Hz |
| `0x0902` | 96 B | 12 个关节位置 | 100 Hz |
| `0x0903` | 96 B | 12 个关节速度 | 100 Hz |
| `0x0904` | 4 B | 状态字（未解） | 100 Hz |
| `0x0905` | 48 B | 手柄：6 个 double，摇杆时非零 | 100 Hz |
| `0x0906` | 368 B | 未解，约 660 Hz | — |
| `0x10901` | 40 B | 未解，约 200 Hz | — |

`0x0901` 的字段偏移与厂商 `qnx2ros.cpp` 中的 `#pragma pack(4)` 结构体逐字节核对过；
`basic=6` 对应「站立待机」，文档没写，是实测反推的。
