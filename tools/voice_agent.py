"""Run the resident voice agent: "小狗 ..." -> plan -> move -> spoken reply.

    python tools/voice_agent.py --device 9 --channels 2 --allow-motion

Without --allow-motion the agent only understands and speaks; it never sends a
velocity command.  Motion runs in a worker thread so a spoken "停" is honoured
while the dog is walking.

Stop phrase handling is the only path that does not need the wake word, and it
also sends an explicit zero-velocity frame on top of ``request_stop()``.
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
import socket
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.app.dog_state import StateFeed  # noqa: E402
from host.app.motion_exec import (  # noqa: E402
    MotionExecutor,
    MotionRefused,
    VelocitySender,
)
from host.app.voice.agent import VoiceAgent, describe  # noqa: E402
from host.app.voice.asr import MODEL_DIR, ChineseAsr  # noqa: E402
from host.app.voice.intent import parse_intent  # noqa: E402
from host.app.voice.mic import record_with_vad_endpoint  # noqa: E402

LOG = logging.getLogger("voice_agent")

# Closed-loop approach (2026-10-27, real dog): the card measured 96 px wide when
# it was still too small for ``read_digits`` to find a glyph and 124-129 px when
# it read at 0.97-0.99, so the dog stops once the card is this wide.  Every leg
# is preceded by a control-mode frame: the dog drops our velocity stream after a
# stop, and the first command after backing up was ignored 3 times in a row.
APPROACH_TARGET_PX = 120
APPROACH_MAX_PX = 320  # hush: do not crawl into the card if it fills the view
APPROACH_STEP_M = 0.15
APPROACH_MAX_STEPS = 6
MODE_AUTONOMOUS = 0x21010C03

# Everything the runner says out loud lives here, because docs/voice_commands.md
# is the single source of truth for what the operator hears (conventions §11) and
# tests/test_voice_commands_doc.py fails until each line is in that doc.
APPROACH_ACK = "好，我走近点看看"
APPROACH_NO_CARD = "没看到数字，是不是放偏了？"
APPROACH_REFUSED = "暂时不能移动，先不靠近了"

# The rest of what the runner says.  Same rule: docs/voice_commands.md lists every
# template, and the doc test fails when one of these is not there.
REPLY_CAMERA_DOWN = "我看不到，相机好像没连上"
REPLY_APPROACH_CRASH = "出了点问题，%s"
REPLY_GOTO_NO_MOTION = "要去找数字{}，得先开运动许可"
REPLY_BUSY = "还在走呢，等它走完"
REPLY_GOTO_OK = "好的，我去找数字{}"
REPLY_READ_DISABLED = "看到数字了，但没开读数字功能"
REPLY_STOP = "停"
REPLY_UNKNOWN = "没听懂，再说一遍"

# Sweep-and-find ("小狗，找数字八"): the dog turns through a full circle and reads
# every card it passes until it sees the asked-for digit.  Same doc rule applies.
SEEK_ACK = "好，我转一圈找数字{}"
SEEK_FOUND = "找到了，数字{}在这边"
SEEK_MISS = "没找到数字{}，我看到的数字有{}"
SEEK_TURN_FAILED = "我转不动了，帮我看看手柄是不是还抓着、或者状态流断了"
SEEK_CRASH = "找数字的时候出了点问题：%s"
SEEK_NO_MOTION = "要转圈找数字，得先开运动许可"
SEEK_CN = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")




def _setup_logging(path: Path) -> None:
    # Windows consoles default to GBK; force UTF-8 so Chinese log lines and
    # TTS replies never crash on an unencodable character.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - older/odd streams
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [voice_agent] %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.messages: "queue.Queue[str]" = queue.Queue()
        self.speaker = None
        if not args.no_tts:
            from host.app.voice.tts import Tts

            self.speaker = Tts()
        self.asr = ChineseAsr(model_dir=args.model_dir or MODEL_DIR)
        self.feed = None
        self.sender = None
        self.executor = None
        self.worker = None
        self.want_read = False
        if args.allow_motion:
            self.feed = StateFeed(port=args.state_port).start()
            self.sender = VelocitySender(board=args.board, port=args.cmd_port)
            self.executor = MotionExecutor(
                self.sender, self.feed, allow_motion=True, period_s=args.period
            )

    # -- speech out -------------------------------------------------------
    def speak(self, text: str) -> None:
        LOG.info("SPEAK: %s", text)
        if self.speaker is not None and text:
            self.speaker.speak(text, async_=True)

    def drain_messages(self) -> None:
        while True:
            try:
                self.speak(self.messages.get_nowait())
            except queue.Empty:
                return

    # -- capture ----------------------------------------------------------
    def capture(self, seconds: float):
        self.drain_messages()
        return record_with_vad_endpoint(
            max_seconds=seconds,
            extend_seconds=self.args.mic_extend,
            device=self.args.device,
            channels=self.args.channels,
            stop_flag=lambda: not self.messages.empty(),
        )

    def transcribe(self, audio) -> str:
        text = self.asr.transcribe(audio, 16000)
        LOG.info("HEARD: %s", text)
        return text

    # -- motion -----------------------------------------------------------
    def _fetch_worker(self, target: int) -> None:
        """去某号 = 转圈找到写着该数字的卡片，再朝它走近读出来.

        Semantics changed 2026-11-01 (user request): there are no equidistant
        stations any more -- "去8号" means "go to the card that shows an 8",
        so the old ``distance = 0.6 m * N`` walk is gone.
        """
        if not self._hunt(target):
            return  # _hunt already queued the "did not find it" sentence
        self.messages.put(SEEK_FOUND.format(SEEK_CN[target]))
        self.messages.put(self._walk_to_card())

    def _read_line(self) -> str:
        """Read the card once (after the body settles) and return the sentence."""
        from host.app.vision.digit_service import read_burst, read_settled, spoken

        def once():
            # Stop-then-shoot: a short burst, then keep only the sharp frames.
            return read_burst(shots=self.args.shots)

        try:
            # A still body is the difference between "1" and "2" (runbook §14):
            # wait for the walk's motion to decay, then retry once if unsure.
            result = read_settled(
                once,
                feed=getattr(self, "feed", None),
                settle_s=self.args.settle,
                retries=self.args.retries,
            )
        except Exception as exc:  # camera down / board unreachable: keep listening
            LOG.error("read failed: %s", exc)
            return REPLY_CAMERA_DOWN
        LOG.info("read result: %s", json.dumps(result.to_dict(), ensure_ascii=False))
        print("READ:", json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
        return spoken(result)

    # -- walk to the card, then read --------------------------------------
    def _send_mode(self, code: int) -> None:
        """Re-send the control-mode frame; the dog wants it before every motion."""
        import socket
        import struct

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(struct.pack("<3I", code, 0, 0), (self.args.dog_host, 43893))
        finally:
            sock.close()

    @staticmethod
    def _card_width():
        """Width of the detected card in pixels, or None when no card is seen."""
        from host.app.vision.camera_client import grab_frame
        from host.app.vision.digit_reader import find_card, to_gray

        try:
            frame = grab_frame()
        except Exception as exc:  # camera down: reading will say so
            LOG.error("camera grab failed: %s", exc)
            return None
        box = find_card(to_gray(frame))
        return None if box is None else box[2] - box[0]

    def _leg(self, step=None):
        """One approach leg, retried once when the dog ignores the first command."""
        distance = self.args.approach_step if step is None else step
        try:
            result = self.executor.move(distance, speed=self.args.speed)
            if result.travelled_m < 0.02:
                LOG.warning("approach leg ignored (%s) - re-arming", result.reason)
                self._send_mode(MODE_AUTONOMOUS)
                time.sleep(0.5)
                result = self.executor.move(distance, speed=self.args.speed)
            return result
        except MotionRefused as exc:
            LOG.error("approach refused: %s", exc)
            return None

    def _approach_step_for(self, width):
        """Smaller steps once the card is already big.

        The demo line is "found it -> take one step -> read", but the 2026-11-01
        data says a card over ~200 px starts coming back "blurred".  So the forced
        step shrinks as the card grows instead of walking a flat 0.15 m.
        """
        base = float(self.args.approach_step)
        if width is None or width < 150:
            return base
        if width < 240:
            return min(base, 0.10)
        return min(base, 0.08)

    def _walk_to_card(self) -> str:
        """Take at least one step toward the card, then read it."""
        width = self._card_width()
        LOG.info("approach: card width %s px (target %d)", width, self.args.approach_px)
        if width is None:
            # hunt saw it seconds ago; if it is gone now, do not walk blindly.
            LOG.warning("approach: card vanished after the sweep - not stepping")
            return APPROACH_NO_CARD
        if width > self.args.approach_max_px:
            LOG.info("approach: card already fills the view - reading in place")
            return self._read_line()
        misses = 0
        taken = 0
        for step in range(1, self.args.approach_steps + 1):
            if taken > 0 and width is not None and width >= self.args.approach_px:
                LOG.info("approach: %d px is close enough", width)
                break
            if width is None and taken > 0:
                misses += 1
                if misses >= 3:
                    return APPROACH_NO_CARD
            self._send_mode(MODE_AUTONOMOUS)
            time.sleep(0.3)
            result = self._leg(self._approach_step_for(width))
            if result is None:
                return APPROACH_REFUSED
            taken += 1
            width = self._card_width()
            LOG.info("approach leg %d: %.3f m, card %s px", step,
                     result.travelled_m, width)
            if width is not None and width > self.args.approach_max_px:
                LOG.info("approach: card fills the view, stopping before it")
                break
            time.sleep(0.3)
        return self._read_line()

    def _needs_approach(self) -> bool:
        """True when the card is missing from the view or still too small to read."""
        width = self._card_width()
        LOG.info("read intent: card width %s px (target %d)", width, self.args.approach_px)
        return width is None or width < self.args.approach_px

    def _approach_worker(self) -> None:
        try:
            self.messages.put(self._walk_to_card())
        except Exception as exc:  # never kill the listening thread
            LOG.exception("approach failed")
            self.messages.put(REPLY_APPROACH_CRASH % exc)

    def _seek_worker(self, want: int) -> None:
        """Turn a full circle looking for one digit, then report what it saw."""
        if self._hunt(want):
            self.messages.put(SEEK_FOUND.format(SEEK_CN[want]))

    def _hunt(self, want: int) -> bool:
        """Sweep the circle for one digit; queue a sentence when it is missing.

        Sweeping only rotates the body: the blind 0.40 m approach leg tried on
        2026-10-28 ended 0.28 m from an obstacle, so hunting never walks.  Every
        stop re-sends the control-mode frame -- the dog drops our velocity stream
        after a stop, and a silently ignored turn made 5 of 9 stops lie earlier.
        """
        from host.app.motion_exec import _yaw_deg
        from host.app.vision.camera_client import grab_frame
        from host.app.vision.digit_reader import find_card, to_gray
        from host.app.vision.digit_service import read_burst
        from host.app.vision.seek import SWEEP_OFFSETS_DEG, centre_card, hunt_for_digit

        ex = self.executor

        def arm() -> None:
            self._send_mode(MODE_AUTONOMOUS)
            time.sleep(0.3)

        def look():
            try:
                return find_card(to_gray(grab_frame()))
            except Exception as exc:  # camera dropped mid-sweep
                LOG.error("seek look failed: %s", exc)
                return None

        def read():
            """One burst per stop; a *near miss* gets one confirming look.

            2026-11-01 15:25: the 8 at the first stop came back at 0.821 against a
            0.85 bar and was thrown away -- "I can see it but you say you cannot".
            A second look that agrees is cheap, and only runs when the digit is
            already the one we want, so it cannot turn a confident 5 into an 8.
            """
            try:
                reading = read_burst(shots=self.args.shots)
            except Exception as exc:
                LOG.error("seek read failed: %s", exc)
                return "", 0.0
            digits = reading.digits or ""
            conf = float(reading.confidence or 0.0)
            if digits == str(want) and conf < self.args.seek_confidence:
                try:
                    again = read_burst(shots=self.args.shots)
                except Exception as exc:
                    LOG.error("seek confirm failed: %s", exc)
                    return digits, conf
                if (again.digits or "") == digits:
                    conf = max(conf, float(again.confidence or 0.0))
                    LOG.info("seek: %r confirmed by a second look (%.2f)", digits, conf)
                    conf = max(conf, self.args.seek_confidence)
                else:
                    LOG.info("seek: %r then %r - not confirmed", digits, again.digits)
                    return "", 0.0
            return digits, conf

        start_state = ex.feed.latest()[0]
        start_yaw = float(_yaw_deg(start_state))
        sweep = {"attempted": 0, "missed": 0}

        def turn_to(offset_deg: float) -> None:
            """Turn to a sweep stop and *notice* when the dog did not turn.

            2026-11-01 15:49: all 8 stops re-read the same "3" and the whole circle
            took 13 s.  ``turn_to_heading`` fails with "not responding" (manual
            mode / control not granted) by returning ``ok=False`` instead of
            raising, and the old code ignored that -- so the sweep silently became
            "read the same card eight times" and ended with "没找到数字八".
            """
            target = start_yaw + float(offset_deg)
            for attempt in (1, 2):
                arm()
                try:
                    result = ex.turn_to_heading(target)
                except Exception as exc:  # MotionRefused / stale state
                    LOG.error("seek turn failed (attempt %d): %s", attempt, exc)
                    result = None
                if getattr(result, "ok", False):
                    sweep["attempted"] += 1
                    return
                LOG.warning("seek: turn to %+0.0f deg not ok (attempt %d): %s",
                            offset_deg, attempt, getattr(result, "reason", result))
                time.sleep(0.3)
            sweep["attempted"] += 1
            sweep["missed"] += 1

        def turn_by(angle_deg: float) -> None:
            arm()
            try:
                ex.turn(float(angle_deg))
            except Exception as exc:
                LOG.error("seek aim failed: %s", exc)

        def centre(_box):
            result = centre_card(look, turn_by)
            return result.box if getattr(result, "ok", False) else None

        result = hunt_for_digit(
            look, centre, read, turn_to, str(want),
            offsets=SWEEP_OFFSETS_DEG,
            min_confidence=self.args.seek_confidence,
            approach=None,      # never walk while hunting (2026-10-28 near-miss)
        )
        LOG.info("hunt %s result: %s", want, result)
        if result.found:
            return True
        if sweep["missed"] >= 3:
            LOG.warning("seek: %d of %d stops never turned - aborting the sweep",
                        sweep["missed"], sweep["attempted"])
            self.messages.put(SEEK_TURN_FAILED)
            return False
        seen = sorted({d for _o, d, _c in (result.seen or []) if d})
        self.messages.put(SEEK_MISS.format(SEEK_CN[want], "、".join(seen) or "没看清"))
        return False

    def handle(self, text: str):
        intent = parse_intent(text)
        LOG.info("INTENT: %s", intent)
        actions = intent.get("actions") or []
        if intent.get("intent") == "goto" and intent.get("target"):
            target = int(intent["target"])
            if not self.args.allow_motion:
                return REPLY_GOTO_NO_MOTION.format(SEEK_CN[target])
            if self.worker is not None and self.worker.is_alive():
                return REPLY_BUSY
            self.worker = threading.Thread(
                target=self._fetch_worker, args=(target,), daemon=True
            )
            self.worker.start()
            return REPLY_GOTO_OK.format(SEEK_CN[target])
        if intent.get("intent") == "seek" and intent.get("target") is not None:
            want = int(intent["target"])
            if not self.args.allow_motion:
                return SEEK_NO_MOTION
            if self.worker is not None and self.worker.is_alive():
                return REPLY_BUSY
            self.worker = threading.Thread(
                target=self._seek_worker, args=(want,), daemon=True
            )
            self.worker.start()
            return SEEK_ACK.format(SEEK_CN[want])
        if intent.get("intent") == "read":
            if not self.args.enable_read:
                return REPLY_READ_DISABLED
            if (self.args.allow_motion and self.executor is not None
                    and self._needs_approach()):
                if self.worker is not None and self.worker.is_alive():
                    return REPLY_BUSY
                self.worker = threading.Thread(
                    target=self._approach_worker, daemon=True
                )
                self.worker.start()
                return APPROACH_ACK
            return self._read_line()
        if intent.get("intent") == "stop":
            self.stop_now()
            return REPLY_STOP
        return REPLY_UNKNOWN

    def stop_now(self) -> None:
        LOG.warning("STOP requested")
        if self.executor is not None:
            self.executor.request_stop()
        if self.sender is not None:
            try:
                self.sender.send(0.0, 0.0, 0.0)
            except Exception:  # pragma: no cover - best effort
                pass


def _single_instance(port: int = 43919):
    """Refuse a second agent: two of them fight over the mic, camera and dog.

    2026-11-01 15:29 two windows ran at once; the loser logged "camera grab
    failed", "seek turn failed: dog state is 18.70 s old" and "seek look failed:
    timed out" while the winner tried to drive.  A bound loopback port is a
    cheap, machine-local lock that needs no file cleanup after a crash.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="resident voice agent")
    ap.add_argument("--device", type=int, default=9)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--mic", type=float, default=8.0, help="max seconds per turn")
    ap.add_argument("--mic-extend", type=float, default=4.0,
                    help="extra seconds captured when speech runs past --mic")
    ap.add_argument("--settle", type=float, default=1.5,
                    help="wait this long after motion stops before reading")
    ap.add_argument("--retries", type=int, default=1,
                    help="extra card-reading bursts when unsure")
    ap.add_argument("--speed", type=float, default=0.10)
    ap.add_argument("--board", default="192.168.1.100")
    ap.add_argument("--cmd-port", type=int, default=43902)
    ap.add_argument("--state-port", type=int, default=43900)
    ap.add_argument("--period", type=float, default=0.05)
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--dog-host", default="192.168.1.120",
                    help="the dog itself: where control-mode frames go")
    ap.add_argument("--approach-px", type=int, default=APPROACH_TARGET_PX,
                    help="walk up to the card until it is this many pixels wide")
    ap.add_argument("--approach-max-px", type=int, default=APPROACH_MAX_PX,
                    help="stop earlier if the card gets bigger than this")
    ap.add_argument("--approach-step", type=float, default=APPROACH_STEP_M,
                    help="metres per approach leg")
    ap.add_argument("--approach-steps", type=int, default=APPROACH_MAX_STEPS,
                    help="give up (and just read) after this many legs")
    ap.add_argument("--allow-motion", action="store_true")
    ap.add_argument("--seek-confidence", type=float, default=0.80,
                    help="accept a swept digit at or above this confidence "
                         "(0.80 since 2026-11-01: a real 8 scored 0.821)")
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--shots", type=int, default=3,                    help="frames per card reading")
    ap.add_argument("--no-read", dest="enable_read", action="store_false",
                    help="disable the camera/digit half")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after N seconds (0 = until Ctrl+C)")
    ap.add_argument("--log", default=None)
    args = ap.parse_args(argv)

    stamp = time.strftime("%Y%m%d")
    _setup_logging(Path(args.log or ROOT / "logs" / ("voice_agent_%s.log" % stamp)))
    lock = _single_instance()
    if lock is None:
        LOG.error("another voice agent is already running - refusing to start")
        print("已经有一个语音代理在运行，先关掉那个窗口再启动这个。")
        return 3
    LOG.info("starting; %s", describe())
    LOG.info("motion allowed: %s", args.allow_motion)

    runner = Runner(args)
    agent = VoiceAgent(
        capture=runner.capture,
        transcribe=runner.transcribe,
        handle=runner.handle,
        speak=runner.speak,
        stop_now=runner.stop_now,
        mic_seconds=args.mic,
    )
    warm = runner.asr.warm_up()
    LOG.info("ASR warm-up %.2fs", warm)
    runner.speak("我在听")
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    try:
        turns = agent.run_until(deadline=deadline)
    except KeyboardInterrupt:
        turns = agent.turns
    finally:
        if runner.feed is not None:
            runner.feed.close()
        if runner.sender is not None:
            runner.sender.close()
    LOG.info("stopped after %d handled turns", turns)
    return 0


if __name__ == "__main__":
    sys.exit(main())
