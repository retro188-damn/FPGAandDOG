"""Speech-input half of the robot command chain: microphone -> Chinese text -> intent.

Sub-modules are NOT imported here on purpose, so that ``import host.app.voice``
stays cheap (no numpy/scipy/sherpa_onnx import, ~0 ms):

* ``asr``    -- offline Paraformer Chinese ASR (sherpa-onnx), lazily loaded.
* ``mic``    -- best-effort microphone capture (needs the optional `sounddevice`).
* ``intent`` -- pure-function parser: text -> structured command dict.
* ``demo``   -- CLI entry point (``python -m host.app.voice.demo --selftest``).

Use the sub-modules directly::

    from host.app.voice.intent import parse_intent
    from host.app.voice.asr import ChineseAsr

The two one-liners below are only a convenience for interactive use
(``from host.app.voice import ChineseAsr``); they still import lazily.

Log lines everywhere in this package follow the project convention::

    [ISO8601] [LEVEL] [module] message
"""
import logging
from datetime import datetime
from typing import Any

__all__ = ["setup_logging", "Iso8601Formatter", "ChineseAsr", "parse_intent", "record"]

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(module)s] %(message)s"


class Iso8601Formatter(logging.Formatter):
    """Formatter that renders `asctime` as an ISO-8601 local timestamp."""

    def formatTime(self, record, datefmt=None):  # type: ignore[override]
        stamp = datetime.fromtimestamp(record.created).astimezone()
        return stamp.isoformat(timespec="milliseconds")


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging with the project line format, once.

    Library modules never call this; only the CLI (`demo.py`) and scripts do.
    Calling it twice is harmless (basicConfig is a no-op once handlers exist).
    """
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    handler = logging.StreamHandler()  # stderr: keeps stdout clean for JSON
    handler.setFormatter(Iso8601Formatter(LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)


def __getattr__(name: str) -> Any:
    """Resolve the lazy convenience names on first access (PEP 562)."""
    if name == "ChineseAsr":
        from host.app.voice.asr import ChineseAsr

        return ChineseAsr
    if name == "parse_intent":
        from host.app.voice.intent import parse_intent

        return parse_intent
    if name == "record":
        from host.app.voice.mic import record

        return record
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
