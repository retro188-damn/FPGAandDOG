"""Resident voice agent: wake word, then one command, forever.

The operator asked (2026-10-26): "how do I get the PC to listen on its own
instead of you opening a listening window every time".  The answer is this
loop plus a long-running process; ASR, intent parsing and motion are unchanged.

Safety model (agreed 2026-10-26, option "A: wake word 小狗 + next sentence"):

* Nothing is executed unless the transcript starts with the wake word 小狗.
* "小狗" alone arms the agent for exactly one more utterance, then disarms.
* A stop phrase ("停", "别动", "急停", ...) is honoured with or without the wake
  word and goes straight to the motion abort.
* The PL safety gate still needs a fresh command every 200 ms, so a crashed
  agent stops the dog by itself.

Every side effect is injected as a callable, so the loop is testable without a
microphone, a model or a robot.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional, Tuple

LOG = logging.getLogger(__name__)

# "小狗" plus the ways ASR actually spells it in noisy audio.
WAKE_WORDS: Tuple[str, ...] = (
    "小狗小狗", "小狗狗", "小狗", "小勾", "小够", "肖狗",
)
EXIT_WORDS: Tuple[str, ...] = ("退出监听", "退出", "结束", "下班", "关闭监听")
STOP_WORDS: Tuple[str, ...] = (
    "停下", "停下来", "停止", "别动", "不要动", "急停", "站住", "停",
)
_PUNCT = " \t\r\n，,。.、！!？?…~～\"'“”‘’"

# parse() outcomes
EXECUTE = "execute"   # wake word + command in one breath
ARM = "arm"           # wake word alone: take the next utterance
STOP = "stop"         # stop phrase
IGNORE = "ignore"     # no wake word
EXIT = "exit"         # wake word + exit phrase: leave the loop


def _clean(text: str) -> str:
    return "".join("" if ch in _PUNCT else ch for ch in (text or ""))


def strip_wake_word(text: str) -> Tuple[bool, str]:
    """Return ``(heard_wake_word, rest)``; ``rest`` is the text after it."""
    clean = _clean(text)
    if not clean:
        return False, ""
    # The wake word must be at the start: a sentence that merely mentions a dog
    # must not fire.
    for word in WAKE_WORDS:
        if clean.startswith(word):
            return True, clean[len(word):]
    # Tolerate one short filler before the wake word ("那个小狗").
    for word in WAKE_WORDS:
        idx = clean.find(word)
        if 0 <= idx <= 2:
            return True, clean[idx + len(word):]
    return False, ""


def is_stop_command(text: str) -> bool:
    """True only when the utterance *starts* with a stop word.

    2026-10-26 live run: the ambient phrase "哎我看怎么停下来了" contains 停 and
    stopped the dog mid-sentence; requiring the stop word up front keeps real
    shouts ("停!", "小狗停下") while ignoring commentary.
    """
    clean = _clean(text)
    if not clean:
        return False
    if any(clean.startswith(word) for word in STOP_WORDS):
        return True
    heard, rest = strip_wake_word(clean)
    return heard and any(rest.startswith(word) for word in STOP_WORDS)


# Read-only commands may skip the wake word: they cannot move the dog, and in
# the 2026-10-26 live run the user kept saying "看看是什么数字" on its own and
# got silence every time. Movement still needs the wake word.
_READ_TRIGGER_WORDS = ("看看", "看一下", "看下", "读一下", "读读", "读", "念", "识别", "扫")
_READ_QUESTION_RE = re.compile(r"(?:是|有)多少|是几|写了什么|写的什么|什么数")


def is_bare_read_command(text: str) -> bool:
    """True for a read-only request that arrived without the wake word."""
    clean = _clean(text)
    if not clean:
        return False
    # Either an explicit read verb, or a real question about the digits. The
    # question form matters because ASR mangles the verb: the 21:04 run turned
    # "看看是什么数字" into "共是什么数据", which still carries the question.
    if not (any(word in clean for word in _READ_TRIGGER_WORDS)
            or _READ_QUESTION_RE.search(clean)):
        return False
    try:  # local import: agent must stay importable without the intent module
        from .intent import parse_intent
    except Exception:  # pragma: no cover - defensive
        return False
    try:
        return parse_intent(clean).get("intent") == "read"
    except Exception:  # pragma: no cover - defensive
        return False


def parse(text: str) -> Tuple[str, str]:
    """Classify one transcript into (action, command_text)."""
    if is_stop_command(text):
        return STOP, _clean(text)
    heard, rest = strip_wake_word(text)
    if not heard:
        if is_bare_read_command(text):
            return EXECUTE, _clean(text)
        return IGNORE, ""
    if rest and any(rest.startswith(word) for word in EXIT_WORDS):
        return EXIT, rest
    return (EXECUTE, rest) if rest else (ARM, "")


class VoiceAgent:
    """Listen -> wake word -> transcribe -> act -> speak, on repeat.

    Injected callables:
      ``capture(max_seconds)``   one utterance (empty result = silence)
      ``transcribe(audio)``      ASR
      ``handle(text)``           executes a command, returns the spoken reply
                                 (None = stay silent)
      ``speak(text)``            TTS
      ``stop_now()``             immediate motion abort
    """

    def __init__(
        self,
        capture: Callable[[float], Any],
        transcribe: Callable[[Any], str],
        handle: Callable[[str], Optional[str]],
        speak: Callable[[str], Any],
        stop_now: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        idle_log_every_s: float = 60.0,
        mic_seconds: float = 8.0,
    ) -> None:
        self.capture = capture
        self.transcribe = transcribe
        self.handle = handle
        self.speak = speak
        self.stop_now = stop_now or (lambda: None)
        self.clock = clock
        self.idle_log_every_s = idle_log_every_s
        self.mic_seconds = mic_seconds
        self.running = False
        self.turns = 0
        self.ignored = 0
        self.last_text = ""

    # -- one transcript ---------------------------------------------------
    def handle_text(self, text: str) -> Optional[str]:
        self.last_text = text or ""
        action, cmd = parse(text)
        if action == STOP:
            LOG.warning("stop command: %r", text)
            self.stop_now()
            self.turns += 1
            return "停"
        if action == EXIT:
            LOG.info("exit requested: %r", text)
            self.running = False
            self.turns += 1
            return "\x00exit"
        if action == IGNORE:
            self.ignored += 1
            LOG.info("ignored (no wake word): %r", text)
            return None
        if action == ARM:
            LOG.info("wake word heard, waiting for the command")
            return "\x00arm"  # sentinel: caller grabs one more utterance
        reply = self.handle(cmd)
        self.turns += 1
        return reply

    # -- main loop --------------------------------------------------------
    def run_until(self, deadline: Optional[float] = None) -> int:
        """Block until ``deadline`` (monotonic) or ``stop()``. Returns turns."""
        self.running = True
        last_idle = self.clock()
        try:
            while self.running:
                if deadline is not None and self.clock() >= deadline:
                    break
                audio = self.capture(self.mic_seconds)
                if audio is None or getattr(audio, "size", 1) == 0:
                    now = self.clock()
                    if now - last_idle >= self.idle_log_every_s:
                        LOG.info("idle, still listening (turns=%d)", self.turns)
                        last_idle = now
                    continue
                text = self.transcribe(audio)
                if not text:
                    continue
                reply = self.handle_text(text)
                if reply == "\x00arm":
                    # Second half of a two-utterance command: no wake word
                    # needed this time, and no re-arming. A second wake word
                    # ("小狗" ... "小狗小狗") just keeps waiting instead of
                    # answering "没听懂".
                    for _attempt in range(2):
                        audio2 = self.capture(self.mic_seconds)
                        if audio2 is None or getattr(audio2, "size", 1) == 0:
                            LOG.info("armed but heard nothing, disarming")
                            reply = None
                            break
                        text2 = self.transcribe(audio2)
                        if not text2:
                            reply = None
                            break
                        action2, _cmd2 = parse(text2)
                        if action2 == ARM:
                            LOG.info("armed again, still waiting")
                            continue
                        reply = self.handle_text(text2) if action2 == EXECUTE \
                            else self.handle(text2)
                        break
                    else:
                        LOG.info("armed twice without a command, disarming")
                        reply = None
                if reply == "\x00exit":
                    self.speak("好的，我退出")
                    break
                if reply:
                    self.speak(reply)
        finally:
            self.running = False
        return self.turns

    def stop(self) -> None:
        self.running = False


def describe() -> str:
    """One-line banner used by the CLI runner and the log header."""
    return "wake words: %s | stop words: %s" % (
        " ".join(WAKE_WORDS[:3]), " ".join(STOP_WORDS[:4])
    )
