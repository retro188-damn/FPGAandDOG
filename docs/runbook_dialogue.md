# 运行手册：上位机对话闭环（不依赖狗 / 不依赖 ZYNQ）

## 它解决什么问题

演示的一半是「说话 → 理解 → 回话」。这部分完全跑在笔记本上，狗和板子拔了也能演示、能回归测试。
它把硬件依赖收成了两类显式的占位：

| 环节 | 现状 | 谁来补 |
|---|---|---|
| 移动到 N 号位置 | `MOVE` 步骤，只输出 JSON | 狗的速度通道（P0-2 动测之后） |
| 读取板子上的数字 | `READ_DIGITS` 步骤 | 视觉（待定放 103 / 120） |
| 播报 | 真播（SAPI 中文语音） | 已完成 |

## 三个新模块

| 文件 | 职责 |
|---|---|
| `host/app/voice/tts.py` | 离线中文播报。SAPI「Microsoft Huihui」，win32com 调用，PowerShell 兜底；`save_wav()` 可导出 22 kHz WAV 供视频/证据 |
| `host/app/pipeline.py` | 意图 → 任务计划（`steps`）+ 要说的话（`say`）。步骤种类：`MOVE / READ_DIGITS / STOP / STAND / LIE / SPEAK` |
| `host/app/assistant.py` | 对话闭环 CLI：录音 → ASR → 意图 → 计划 → 播报 |

`--digit` 用来假装"看完了、板子上是 4"，把最后一句也说出来，演示时不需要真的摄像头。

## 怎么跑

```bash
# 1) 纯文本（调试意图/话术，不碰麦克风）
PYTHONIOENCODING=utf-8 python -m host.app.assistant --text "去三号位置看看是什么数字" --digit 47

# 2) 真麦克风 + 自动断句 + 自动校准环境声
PYTHONIOENCODING=utf-8 python -m host.app.assistant --mic 10 --mic-vad --device 9 --channels 2

# 3) 不播报、只打印（做回归测试用）
PYTHONIOENCODING=utf-8 python -m host.app.assistant --mic 10 --mic-vad --device 9 --channels 2 --no-tts

# 4) 只测播报
PYTHONIOENCODING=utf-8 python -m host.app.voice.tts "收到，前往三号位置"
```

## 输入 / 输出示例

```
$ ... --text "去三号位置看看是什么数字" --digit 47
[INFO] [assistant] transcript: 去三号位置看看是什么数字
{"intent": "goto", "target": 3, "actions": ["read_digits"],
 "say": ["收到，前往三号位置", "正在查看三号位置"],
 "steps": [{"kind": "MOVE", "target": 3, "payload": {"station": 3}},
           {"kind": "SPEAK", "text": "收到，前往三号位置"},
           {"kind": "READ_DIGITS", "target": 3},
           {"kind": "SPEAK", "payload": {"slot": "digit_result"}}],
 "requires_hardware": true}
```
播报内容：`收到，前往三号位置` → `正在查看三号位置` → `板子上写的是四七`。

## 语音端点（VAD）的自适应

这个阵列的录音电平在不同次之间能差 20 倍（peak 0.011 ~ 0.32），固定阈值要么漏掉小声、
要么被噪声触发。现在 `--vad-threshold 0`（默认）会先用开头 0.5 s 估环境底噪，
触发阈值 = `max(3 × 底噪, 0.003)`：

```
[INFO] [mic] calibrated floor 0.00000 -> speech threshold 0.00300
```

注意：校准窗口内要安静一下，否则底噪会估高。

## 已知问题

1. 内置阵列在安静房间的底噪几乎是 0（`0.00000`），阈值退化成 0.003；正常说话能过，耳语可能过不了。
2. ASR 会把「数字」听成「书」这类近音词；意图解析只依赖动词 + 序号，所以结果仍然正确。
3. `--digit` 是假数据，真实数字要等视觉模块接进来。
4. TTS 走 COM，必须在主线程初始化；异步模式（`speak(..., async_=True)`）已留接口但未使用。

## 下一步接线

1. `MOVE` 步骤 → `host/app/gate_bridge.py` 的 `frames_for_command()`，先打 `--dry-run` 打印帧，再发真板子。
2. `READ_DIGITS` 步骤 → 视觉模块返回字符串，喂 `pipeline.digit_sentence()`。
