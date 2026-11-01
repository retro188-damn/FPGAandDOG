# 运行手册：语音指令输入（mic → 中文文本 → 结构化指令）

## 它解决什么问题

比赛要求「说一句话 → 狗走到指定位置 → 读板子上的两位数 → 说出来」。本模块只做**前半段**：
把麦克风（或 wav 文件）里的中文语音变成一句**结构化的指令 JSON**，交给下游的运动控制。
全程离线：模型、解码、解析都在本机，不联网、不调云 API。

```
麦克风 / wav ──► ChineseAsr（sherpa-onnx Paraformer，本地 int8 模型）
                          │  文本："去3号位置并查看板子上的数字"
                          ▼
                   parse_intent()（纯函数，无 IO）
                          │
                          ▼
        {"intent": "goto", "target": 3, "actions": ["read_digits"], ...}
```

## 文件

| 文件 | 作用 |
|---|---|
| `host/app/voice/asr.py` | `ChineseAsr`：本地 Paraformer 识别，模型**首次调用时才加载**；`load_wav()` 兼容 16 k/8 k、单/双声道，自动重采样到 16 kHz |
| `host/app/voice/mic.py` | `record()` 定长录音、`record_with_vad_endpoint()` 用 numpy RMS 能量做端点检测（说话人停顿约 700 ms 自动停） |
| `host/app/voice/intent.py` | `parse_intent()`：规则解析，纯函数，可穷举单测 |
| `host/app/voice/demo.py` | 命令行入口（`--selftest` / `--wav` / `--mic` / `--text`） |
| `tests/test_intent.py` | 99 个解析用例（整洁指令、噪声、数字、越界、停/站/趴、无关句、读数动作） |

## 依赖

已在开发电脑上验证可用，**不需要**再装：

| 包 | 版本 | 用途 |
|---|---|---|
| `sherpa-onnx` | 1.13.8 | 识别引擎（注意：该版本**没有** `read_wave`，本模块用标准库 `wave` 读 wav） |
| `numpy` | 2.3.5 | 音频数组、RMS 端点检测 |
| `scipy` | 1.16.3 | `resample_poly` 重采样 |

**缺一个包：`sounddevice`（麦克风录音）。** 本仓库不允许自行安装依赖，所以 `mic.py` 是可降级的：
`import host.app.voice.mic` 永远不炸，只有真正调用录音函数时才抛一行英文错误：

```
RuntimeError: microphone capture requires the 'sounddevice' package: pip install sounddevice
```

拿到授权后执行 `pip install sounddevice` 即可，代码不用改。`--wav` / `--selftest` / `--text` 三条路径
完全不依赖 `sounddevice`，现在就能跑。

## 运行

全部在仓库根目录执行（Git Bash）：

```bash
cd "E:/zongy/WORK/Claude code/FPGAandDOG"
```

### 1. 自检：把模型自带 7 个 wav 全跑一遍，打印时长与实时率

```bash
python -m host.app.voice.demo --selftest
```

判据（RTF = 解码耗时 / 音频时长，越小越快）：**RTF 必须远小于 1**，本机实测：

```
load: 1.64s   threads: 4
wav              dur[s]  decode[s]    rtf  text
0.wav              5.61      0.095  0.017  对我做了介绍啊那么我想说的是呢大家如果对我的研究感兴趣呢嗯
1.wav              5.15      0.076  0.015  重点呢想谈三个问题首先呢就是这一轮全球金融动荡的表现
2-zh-en.wav        8.10      0.123  0.015  yesterday was 星期一 today is tuesday 明天是星期三
3-sichuan.wav      7.83      0.194  0.025  自己就是在那个在那个就是在情节里面就是感觉是演的特别好就是好像很真实一样你知道吧
4-tianjin.wav      7.81      0.196  0.025  其实他就是怕每个人就可以守法就这意思法律意识太单薄了而且就是嗯也不顾及到别人的感受
5-henan.wav        7.99      0.213  0.027  他这个管一向都通到有时候都通到七八层楼高然后他这管一向就可以交到那个那柱子上
8k.wav             4.52      0.115  0.026  深入的分析这一次全球金融动荡背后的根源
------------------------------------------------------------------------
wavs=7  failures=0  audio=47.02s  decode=1.012s  mean_rtf=0.022  (model load 1.64s)
```

`8k.wav` 是 8 kHz 采样，走了重采样分支，结果正常 —— 这两点都必须看到，否则说明模型路径或重采样坏了。

### 2. 麦克风（需要先装 `sounddevice`）

```bash
python -m host.app.voice.demo --mic 5          # 录 5 秒后识别
python -m host.app.voice.demo --mic 8 --mic-vad  # 最多 8 秒，说话人停顿约 0.7 s 自动结束
```

没有 `sounddevice` 时退出码 **3**，stderr 只有一行可操作的错误，不会打 traceback。

### 3. 识别已有 wav

```bash
python -m host.app.voice.demo --wav path/to/record.wav
```

### 4. 只测解析器（不碰麦克风、不加载模型）

```bash
python -m host.app.voice.demo --text "去3号位置并查看板子上的数字"
```

### 5. 单测

```bash
python -m pytest tests/test_intent.py -q     # 99 passed
```

## 输出格式（意图 JSON）

`--wav` / `--mic` / `--text` 的 stdout **只有一行** JSON（`ensure_ascii=False`，中文可读），日志全部走 stderr，
所以可以直接 `| jq .` 或用管道喂给下游。

| 字段 | 类型 | 说明 |
|---|---|---|
| `intent` | str | `goto` / `stop` / `stand` / `lie` / `unknown` |
| `target` | int \| null | 只有 `goto` 有值，1..8 |
| `actions` | list[str] | 目前只有 `read_digits`（"顺便读数"） |
| `raw` | str | 原始 ASR 文本（**未**清洗，用于回放/排错） |
| `confidence` | float | 0.0 ~ 0.9，见下表 |

```json
{"intent": "goto",    "target": 3,    "actions": ["read_digits"], "raw": "去3号位置并查看板子上的数字", "confidence": 0.9}
{"intent": "goto",    "target": 3,    "actions": [],              "raw": "去 三 号 位 置", "confidence": 0.9}
{"intent": "goto",    "target": 4,    "actions": ["read_digits"], "raw": "嗯那个请你去第 4 个位置看一下数字", "confidence": 0.9}
{"intent": "stop",    "target": null, "actions": [],              "raw": "停下", "confidence": 0.9}
{"intent": "lie",     "target": null, "actions": [],              "raw": "趴下", "confidence": 0.9}
{"intent": "unknown", "target": null, "actions": [],              "raw": "今天天气不错", "confidence": 0.0}
{"intent": "unknown", "target": null, "actions": [],              "raw": "去9号位置", "confidence": 0.0}
```

置信度是**手调的经验值，不是概率**：

| 值 | 含义 |
|---|---|
| 0.9 | 单一意图、目标短语完整（`3号位置` / `第3个位置`） |
| 0.8 | 有动词有数字，但没有目标短语（裸数字，如 `去3`） |
| 0.6 | 多个意图同时命中（如 `停下然后去3号位置`），按 goto > stop > stand/lie 取值 |
| 0.0 | 没听懂（`unknown`） |

**下游纪律：只在 `intent=="goto"` 时才发运动指令；`unknown` 一律不动，宁可让用户重说。**

## 解析规则（`intent.py`）

1. 归一化：NFKC（全角数字 `３`→`3`）→ 小写 → 去标点空格 → 去语气词（嗯/啊/那个/呃/额/呢）。
2. 数字：阿拉伯数字或中文数字（一二三四五六七八九十、两），支持 `十一`~`九十九`；**只接受 1..8**，其余 → `unknown`。
3. 目标短语（强→弱）：`第N个位置` / `第N个` / `N号位置` / `位置N`、`标号N`、`点N` / 裸数字。
   `一下`、`一起` 里的「一」会被排除，避免把「去位置看一下」误判成去 1 号位。
4. goto 需要**动词 + 可解析数字**同时成立；只有动词没有数字 → `unknown`（0.0），原地不动。
5. `read_digits`：命中 数字/号码/标号/看/读/查/识别/念/读数/多少；被 `不看`/`别读`/`不用识别` 等否定时不算。
6. `停` 但后面是「车」（停车场）不算急停。

## 已知局限

- **`sounddevice` 没装**，所以 `--mic` 现在只能验证到「干净地报错」，真实麦克风链路未跑过。
- 麦克风端点检测是**固定阈值**（RMS 0.015）的玩具级 VAD，不是 webrtcvad；环境噪声大或说话很轻会误判。
  首次现场使用建议先用 `--mic 5` 录一段，按实际情况调 `rms_threshold`。
- 规则解析不处理否定式运动指令：`别去3号位置` 仍会被解析成 `goto 3`（`不要看数字` 已处理）。
  赛场上不要说否定句。
- 只有「位置短语」没有动词（如 `三号位`）会返回 `unknown`，这是刻意的安全选择。
- 识别是**一次性整句**，不是流式；一句最长建议 ≤ 8 s。
- 模型对标准普通话最好，带口音（四川/天津/河南）在自带样例上文字仍然通顺，但**口令仍需现场实测**。
- `scipy` 重采样在 8 kHz 输入下会插值到 16 kHz，识别的字准但音质上限受原始采样率限制。
- Windows 控制台管道：如果中文显示成乱码，加 `PYTHONIOENCODING=utf-8`。

## 日志

所有模块统一：`[ISO8601] [LEVEL] [module] message`，例如

```
[2026-10-25T22:07:53.639+08:00] [INFO] [asr] paraformer model loaded in 1.64s (threads=4)
[2026-10-25T22:08:28.682+08:00] [INFO] [demo] transcript: 深入的分析这一次全球金融动荡背后的根源
```

日志走 stderr，stdout 只有 JSON —— 这点在把本模块接到下游进程时是硬约定。

## 7. 本机麦克风采集：真正的坑是「单声道打开 2 声道阵列」

### 7.1 现象
Windows「设置 → 声音 → 输入」的音量条会动，但 `sounddevice` 有时读出来是**精确的 0**，
有时是一层怎么都去不掉的宽带噪声，ASR 只能输出「嗯」「别给我」这类幻觉。

### 7.2 实测矩阵（device 9 = 麦克风阵列 (Realtek(R) Audio)，48 kHz）

| 模式 | 声道 | rms | peak | 结论 |
|---|---|---|---|---|
| 共享 | 1 | 0.000001 | 0.00003 | **数字静音，坏** |
| 共享 | 2 | 0.0497 | 0.363 | **正常，能直接出识别结果** |
| 独占 | 1 | 0.000534 | 0.0068 | 衰减 100 倍，坏 |
| 独占 | 2 | 0.000541 | 0.0047 | 能用但要放大约 50 倍 |

**根因：这个阵列是 2 声道设备，按 `channels=1` 打开时 PortAudio/Realtek 给出的
是残废数据（共享模式甚至全 0）。之前「共享模式坏了」的结论是错的——那时我只试过单声道。**

### 7.3 现在的默认做法
- **共享模式 + `--channels 2`**（不独占，不需要原生采样率 hack）：`peak≈0.32`，电平正常。
- `mic.py` 的 `_downmix()` 把双声道平均成单声道。
- `_open_rate()` 先试请求的采样率，`check_input_settings` 失败再退回端点原生率并重采样
  （本机 2 声道不接受 16 kHz → `PaErrorCode -9997 Invalid sample rate`）。
- 独占模式仍保留为可选项，仅作排障用。

### 7.4 可用命令
```bash
# 正式闭环（推荐）：共享 + 双声道 + VAD 自动断句
python -m host.app.voice.demo --mic 8 --mic-vad --device 9 --channels 2 --vad-threshold 0.02

# 探针：看电平、存 wav、打印识别文本与意图
python tools/mic_live_probe.py --seconds 8 --device 9 --channels 2 --normalize
```

### 7.5 实测结果（2026-10-25 23:5x）
```
transcript: '一号位置看看是什么数字去一号位置看看是什么数字去'
intent:     {"intent": "goto", "target": 1, "actions": ["read_digits"], "confidence": 0.9}
```
```
transcript: '去三号位置看看是什么书'          # 独占 2ch + 53x 归一化
intent:     {"intent": "goto", "target": 3, "actions": ["read_digits"], "confidence": 0.9}
```

### 7.6 遗留
- 「数字」会被识别成「书」这类近音字；意图解析靠动词+序号，仍能给出 `read_digits`，够用。
- 房间噪声大时建议仍做一次峰值归一化再送 ASR。

## 7b. 2026-10-26 实机复测（重要修正）

- 先用 `--exclusive` 录了两次，**都只拿到静音**（VAD 30 s 内没触发）。全设备扫描显示设备本身正常：
  device 1 rms=0.054 peak=0.44、device 9 rms=0.077 peak=0.50；device 4/5 是数字静音；WDM-KS 设备打不开（驱动限制）。
- 改为**共享模式 + `--channels 2` + `--device 9`** 后一次成功：

```
listening (max 60.0s, stop after 700 ms silence)
calibrated floor 0.00000 -> speech threshold 0.00300
endpoint detected after 3.20s
paraformer model loaded in 1.93s (threads=4)
transcript: 去三号位置看看数字
{"intent": "goto", "target": 3, "actions": ["read_digits"], "confidence": 0.9}
```

结论：**日常不要再加 `--exclusive`**。独占模式在这台机器上会静默吞掉声音，只作为排障手段保留。
录音证据：`metrics/evidence/voice_live_20261026.wav`。

## 8. 常驻监听（resident voice agent）

用途：电脑一直开着听，说「小狗，去一号位置」就执行，不用每次单独开监听窗口。

```bash
# 只用语音、不动狗（安全预览）
python tools/voice_agent.py --device 9 --channels 2

# 完整链路（会真的让狗走）
python tools/voice_agent.py --device 9 --channels 2 --allow-motion
```

唤醒规则（2026-10-26 用户选 A）：

| 说法 | 结果 |
|---|---|
| 「小狗，去一号位置」 | 一句话内唤醒+执行 |
| 「小狗」→ 停顿 →「去三号位置」 | 唤醒后等一句，执行第二句 |
| 「去一号位置」（无唤醒词） | 忽略，不执行 |
| 「停」「停下」「别动」「急停」 | 立刻停车（不需要唤醒词） |
| 其它闲聊 | 忽略（日志记 `ignored`） |

前置条件：狗站立、**自主模式**（`0x21010C03`）、状态分身在线（103:43897 → PC:43900）、
电脑麦克风共享模式 dev 9。手动模式下速度指令会被忽略，执行器 0.6 s 内报
`dog is not responding to velocity commands (manual mode? ...)` 并停车。

日志：`logs/voice_agent_<YYYYMMDD>.log`；退出：Ctrl+C（会关闭 feed/sender，不发速度）。

开机自启（可选，需用户批准再装）：
- Windows 任务计划程序 →「登录时」触发 → 程序 `python`，参数 `tools/voice_agent.py --device 9 --channels 2 --allow-motion`，起始位置填工程根目录。
- 不建服务、不写注册表，删掉任务即回滚。

限制：数字识别尚未接入；`read_digits` 动作目前只回复「已到达N号位置」。

### 8.1 怎么退出

| 方式 | 说明 |
|---|---|
| 说「**小狗，退出**」 | 播报「好的，我退出」后正常退出（`小狗退出`/`小狗下班`/`小狗结束` 同效） |
| `Ctrl+C` | 立即中断，finally 里关闭 feed/sender（不发速度帧） |
| 另一个终端 `taskkill /PID <pid> /F` | 兜底，仅在前两种失效时用 |

「退出」「结束」**必须带唤醒词**才算数，避免闲聊把监听关掉（2026-10-26 用户提出，已加测试）。

## 13. 读数字接进语音链路（2026-10-26）

之前 `read_digits` 只解析、不执行；现在两条路径都接上了相机。

| 入口 | 命令 | 行为 |
|---|---|---|
| 脚本 | `python -m host.app.assistant --text "看看是什么数字" --read-digits` | 打印 `READ: {...}` 并播报结论 |
| 常驻 | `python tools/voice_agent.py --allow-motion` | 说"小狗"唤醒后说"看看是什么数字"即可 |
| 边走边读 | 说"去一号位置看看数字" | 走到位、停稳**之后**再读（运动中的帧会糊） |

### 说出来的三种结果

| 读数 | 播报 |
|---|---|
| 一致且 ≥60% 帧同意 | "板子上写的是**三**" |
| 帧间不一致 | "我看到有点像七，但**不太确定**" |
| 画面里没有字 | "我**没看到数字**，能再靠近一点吗？" |

判据在 `host/app/vision/digit_service.py`：`MIN_AGREEMENT = 0.60`，
`reason ∈ {ok, unclear, empty}`，`to_dict()` 直接进日志和 JSON 契约。

### 代码接口

```python
from host.app.vision.digit_service import read_card, spoken
result = read_card(shots=3)      # 默认走 192.168.1.103:43920
print(result.to_dict())
speak(spoken(result))
```

`grab` / `classifier` / `frames` 都可注入，所以单元测试不需要相机（`tests/test_digit_service.py`）。

## 14. 真机首跑复盘（2026-10-26 20:53–20:56，用户自己跑 `tools/voice_agent.py`）

日志：`logs/voice_agent_20261026.log`（同一文件里 19:17 是早先一轮）。

| 现象 | 次数 | 结论 |
|---|---|---|
| 唤醒成功并执行 | 1 | 「小狗去一号位置」→ 走 0.601 m（6.56 s，130 条命令）→ 播报到达 |
| `ignored (no wake word)` | 9 | 其中 **4 次是「看看是什么数字」** —— 只读命令当时要求唤醒词，全被丢 |
| 回「没听懂，再说一遍」 | 2 | 都是**待命期间又说了唤醒词**（「小狗」→「小狗小狗」）被当成指令 |
| `no speech above rms 0.0150` | 15 | 多数是真没说话（看屏幕的静默期），不是 VAD 故障 |
| 语音退出 | 1 | 「小狗退出」→ 播报「好的，我退出」→ 正常退出 |

两处已修（`host/app/voice/agent.py` + 3 个新测试，全套 **197 passed**）：
1. **只读命令免唤醒词**：`is_bare_read_command()` 要求「动作词 + `intent=read`」双条件，
   「我在看这个数字」这类闲聊不会触发。
2. **待命期再听到唤醒词 → 继续待命**，不再回「没听懂」。

遗留（模型层面，非逻辑）：短句 ASR 不稳（「销购销购」「后位置」「按看是什么数字」）。
缓解：说完整句子、离麦克风近一点、环境安静；必要时改说「小狗，去一号位置看看数字」一整句。

### 14.1 第二轮（21:02–21:04）暴露的三个问题

| 现象 | 根因 | 修复 |
|---|---|---|
| `INTENT: read` 之后**进程直接没了**（日志到 21:04:41 断掉） | `tools/voice_agent.py:135` 写的是 `from vision.digit_service import ...`，而脚本只把仓库根加进 `sys.path`，`vision` 不是顶层包 → `ModuleNotFoundError: No module named 'vision'` | 改成 `from host.app.vision.digit_service import ...` |
| 相机挂了会让整个常驻 agent 崩掉 | 读卡没有异常保护 | `_read_line()` 包 try/except，失败播报「我看不到，相机好像没连上」并继续听 |
| 「共是什么数据」（ASR 把"看看"吞了）被忽略 | 之前要求必须有动作词 | 放行「疑问句形式」（是多少/什么数/写的什么），仍不认「我在看这个数字」这类闲聊 |

回归：`pytest` **199 passed**（新增 verbless-question、runner-import 两项）。

### 14.2 「走完立即读」会读错（2026-10-26 21:09 实跑证据）

同一次会话、同一张卡（纸上是 2）、同一位置，只有"停稳时间"不同：

| 时刻 | 距移动结束 | 读数 | conf | agreement |
|---|---|---|---|---|
| 21:09:16.704 | **0.72 s** | `1` ❌ | 0.796 | 1.0（3 帧全错） |
| 21:10:19.560 | **约 3.5 s** | `2` ✅ | 0.984 | 1.0 |

`move result` 在 21:09:15.864（0.603 m / 6.659 s / 131 条命令），读卡紧跟在 `已到达1号位置` 之后。
用户判断是**刚走完机身/云台/头部俯仰未收敛**，画面还没定住——3 帧一致地错（agreement 1.0）
说明不是随机噪声，而是**系统性地看到了不同的东西**（视野偏移或模糊），与"未停稳"假说一致。

**拟改法（未实施，已记入 `docs/progress.md` 待办 6）**：移动返回后先过 1.5 s 静止窗口
（`vel_body` 三轴 < 0.02 m/s 且卡片框帧间位移 < 3 px）再读；`confidence < 0.9` 或
`agreement < 0.6` 时间隔 1 s 重读一次。验收：连续 10 次"一句走+读"，正确率 ≥ 9/10。

## 15. 录音不再被截断 + 常见 ASR 口误的修复（2026-10-26 夜）

### 15.1 说话晚于窗口末尾被掐断
`record_with_vad_endpoint` 新增 `extend_seconds`（默认 4.0，CLI `--mic-extend`）：
块预算用完时若**仍在说话**，继续录到说话人停顿为止（上限 `max_seconds + extend_seconds`），
日志出现 `window full (...) but still speaking -> extending up to ...`。
单测用脚本化假麦克风覆盖：正常短句仍在第一个停顿处结束、跨窗口的长句被延长、
`--mic-extend 0` 时退回旧行为（`tests/test_mic_endpoint.py` 6 项）。

### 15.2 口误修复表（`intent.py::_ASR_FIXES`，每条都来自真实日志）
| 听成 | 实际 | 例子 |
|---|---|---|
| `后位置` | `号位置` | "去一号**后**位置" → goto 1（之前判 unknown） |
| `什么书` / `什么数子` | `什么数字` | "看看是什么**书**" → read（之前 unknown） |
| `按看` | `看看` | "**按看**是什么数字" → read |
| `位子` | `位置` | "去一号**位子**" → goto 1 |

只修**高置信、可复现**的几种；不动"销购销购"这类整句崩坏（那是收音/识别质量问题，
已在待办 2 里单列）。`tests/test_intent.py` 有对应表驱动测试与"不许凭空造目标"的反向测试。

### 15.3 停稳再读
见 §14 的复盘：走完 0.7 s 读卡会三帧一致地读错。`read_settled()`（`vision/digit_service.py`）
现在先等机身静止（`vel_body` 三轴 < 0.02 m/s 连续 1.5 s，最多等 6 s），再读；
若 `confidence < 0.9` 或 `agreement < 0.6`，隔 1 s 重拍一次取更好者。CLI：`--settle`、`--retries`。

## 16. 静音幻觉与字错率基线（2026-10-26 夜）

### 16.1 问题
用 `tools/asr_eval.py` 回放真机录下的 8 段音频，两段**静音**被识别成英文句子：
`'it is 懂了 and i is 用了'`、`'i fuck me you can make you happy all now'`。
比"没听清"更危险——agent 会拿着这句话去执行。

### 16.2 修复（`host/app/voice/asr.py`，默认开启，可 `guard=False` 关闭）
| 检查 | 阈值 | 作用 |
|---|---|---|
| `is_probably_silence()` | 峰值 < 0.005 | 数字静音直接返回 `""`，**不加载模型** |
| `looks_non_chinese()` | 拉丁字母 ≥ 4 且 > 2×汉字数 | 丢弃噪声转写，记 WARNING |

两条都保守（真指令是纯中文），18 项单测钉住两个方向：噪声必须丢、中文指令不能被丢。

### 16.3 基线数字（`tools/asr_eval.py` + `metrics/asr_manifest.json`）
```
修复前：mean CER 0.3095，静音幻觉 2
修复后：mean CER 0.0238，静音幻觉 0，意图 5/5 正确
worst CER 0.1667（"数字"被听成"书"，意图仍然正确）
```
清单里每条都记了"当时到底说了什么"（对照 `logs/voice_live_20261026.log`），
所以这组数字可以直接写进报告；后续换成 10 条真人指令时只改 manifest，不改代码。

### 16.4 仍然没解决的
- 固定 8 s 窗口已在 `mic.py` 加了 `extend_seconds=4.0` 延长，但**没有真机验证**（狗断电）。
- 距离/噪声对字错率的影响需要真人在场，manifest 已留好接口。
