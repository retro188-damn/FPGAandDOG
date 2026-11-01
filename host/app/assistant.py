"""Host-side dialogue loop: speak a command -> intent -> plan -> spoken reply.

This is the software half of the MVP with every piece of hardware faked away:
no dog, no FPGA, no camera.  It exists so the interaction can be demonstrated
and regression-tested while the robot is unplugged.

Examples::

    python -m host.app.assistant --text "去三号位置看看是什么数字"
    python -m host.app.assistant --mic 8 --device 9 --channels 2
    python -m host.app.assistant --text "去三号位置看看是什么数字" --digit 47
    python -m host.app.assistant --mic 8 --no-tts --save-wav out.wav
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from host.app.pipeline import digit_sentence, plan_from_intent  # noqa: E402
from host.app.voice.intent import parse_intent  # noqa: E402

LOG = logging.getLogger("assistant")


def _configure_console() -> None:
    """Windows consoles default to GBK; Chinese log lines must stay readable."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _listen(args: argparse.Namespace) -> str:
    """Record one utterance and return its transcript ('' when nothing heard)."""
    from host.app.voice.asr import MODEL_DIR, ChineseAsr
    from host.app.voice.mic import record, record_with_vad_endpoint

    if args.mic_vad:
        audio = record_with_vad_endpoint(
            max_seconds=args.mic,
            device=args.device,
            exclusive=args.exclusive,
            channels=args.channels,
            rms_threshold=(args.vad_threshold if args.vad_threshold > 0 else None),
        )
    else:
        audio = record(
            args.mic, device=args.device, exclusive=args.exclusive, channels=args.channels
        )
    if audio.size == 0:
        LOG.warning("no speech captured (try a lower --vad-threshold)")
        return ""
    if args.save_wav:
        from tools.mic_live_probe import save_wav  # reuse the probe helper

        save_wav(args.save_wav, audio, 16000)
        LOG.info("wav saved: %s", args.save_wav)
    asr = ChineseAsr(model_dir=args.model_dir or MODEL_DIR)
    return asr.transcribe(audio, 16000)


def main(argv=None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(description="voice command -> plan -> spoken reply")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="skip ASR and use this transcript")
    source.add_argument("--mic", type=float, metavar="SECONDS", help="record and transcribe")
    parser.add_argument("--mic-vad", action="store_true", help="stop on trailing silence")
    parser.add_argument("--device", type=int, default=9)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--exclusive", action="store_true")
    parser.add_argument(
        "--vad-threshold", type=float, default=0.0,
        help="fixed RMS trigger; 0 (default) auto-calibrates to the room floor",
    )
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--save-wav", default=None)
    parser.add_argument("--digit", default=None, help="pretend the board showed this")
    parser.add_argument("--read-digits", action="store_true",
                        help="run the camera reader instead of --digit")
    parser.add_argument("--shots", type=int, default=3,
                        help="frames per card reading (read mode)")
    parser.add_argument("--camera-host", default=None)
    parser.add_argument("--camera-port", type=int, default=None)
    parser.add_argument("--move", action="store_true",
                        help="drive to the requested station after speaking the acknowledgement")
    parser.add_argument("--approve", action="store_true",
                        help="required with --move: the dog moves (conventions.md section 1)")
    parser.add_argument("--speed", type=float, default=0.10, help="forward speed, m/s")
    parser.add_argument("--board", default="192.168.1.100", help="board running pl_service")
    parser.add_argument("--cmd-port", type=int, default=43902)
    parser.add_argument("--state-port", type=int, default=43900,
                        help="local port receiving the relayed 0x0901 frames")
    parser.add_argument("--period", type=float, default=0.05,
                        help="executor command period, seconds")
    parser.add_argument("--no-tts", action="store_true", help="print instead of speaking")
    parser.add_argument("--rate", type=int, default=0, help="TTS rate -10..10")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )

    if args.text:
        text = args.text
    else:
        text = _listen(args)
    if not text:
        print('{"intent": "unknown", "reason": "no speech"}')
        return 2

    intent = parse_intent(text)
    plan = plan_from_intent(intent)
    LOG.info("transcript: %s", text)
    LOG.info("plan: %s", plan.to_json())

    def speak(line: str) -> None:
        if args.no_tts:
            print("SAY:", line)
            return
        from host.app.voice.tts import Tts

        if not Tts(rate=args.rate).speak(line):
            print("SAY:", line)

    # The acknowledgement comes first, then the move, then the outcome: the
    # operator hears "going" before the dog starts walking.
    for line in plan.say:
        speak(line)

    if args.move and plan.requires_hardware and plan.target:
        _move(args, plan.target, speak)

    if args.digit:
        speak(digit_sentence(args.digit))
    elif args.read_digits or "read_digits" in plan.actions:
        _read(args, speak)
    print(plan.to_json())
    return 0


def _read(args: argparse.Namespace, speak) -> None:
    """Grab a burst from the perception board, read the card, say the number."""
    from host.app.vision import digit_service

    kwargs = {"shots": args.shots}
    if args.camera_host or args.camera_port:
        from host.app.vision.camera_client import CAMERA_HOST, CAMERA_PORT, grab_frame

        host = args.camera_host or CAMERA_HOST
        port = args.camera_port or CAMERA_PORT
        kwargs["grab"] = lambda: grab_frame(host, port)

    result = digit_service.read_burst(**kwargs)
    LOG.info("read result: %s", json.dumps(result.to_dict(), ensure_ascii=False))
    print("READ:", json.dumps(result.to_dict(), ensure_ascii=False))
    if result.reason in ("empty", "blurred"):
        LOG.warning("no digit in view: check that the card is inside the frame "
                    "and lit evenly (runbook_digit.md sections 9-11)")
    speak(digit_service.spoken(result))


def _move(args: argparse.Namespace, target: int, speak) -> None:
    """Preview, then (only with --approve) drive to the station."""
    from host.app.dog_state import StateFeed
    from host.app.motion_exec import (
        STATION_SPACING_M,
        MotionExecutor,
        MotionRefused,
        VelocitySender,
    )

    distance = STATION_SPACING_M * target
    print("MOVE: station %d -> forward %.2f m at %.2f m/s (target %s:%d)"
          % (target, distance, args.speed, args.board, args.cmd_port))
    if not args.approve:
        LOG.warning("move not executed: pass --approve once the operator is ready "
                    "(conventions.md section 1)")
        return

    feed = StateFeed(port=args.state_port).start()
    sender = VelocitySender(board=args.board, port=args.cmd_port)
    ex = MotionExecutor(sender, feed, allow_motion=True, period_s=args.period)
    try:
        result = ex.move_to_station(target, speed=args.speed)
    except MotionRefused as exc:
        LOG.error("move refused: %s", exc)
        speak("暂时不能移动：%s" % exc)
        return
    finally:
        sender.close()
        feed.close()

    LOG.info("move result: %s", result.to_dict())
    if result.ok:
        speak("已到达{}号位置".format(_cn(target)))
    else:
        speak("没走到位，停在{}号位置前".format(_cn(target)))


def _cn(value: int) -> str:
    from host.app.pipeline import cn_number

    return cn_number(value)


if __name__ == "__main__":
    sys.exit(main())
