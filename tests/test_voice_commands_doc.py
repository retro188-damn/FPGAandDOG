"""Keep docs/voice_commands.md in sync with the vocabulary in the code.

If this test fails, either the doc or the code changed alone - update both in
the same commit (conventions.md section 11).

The doc is the *single source of truth for what the operator hears*, so it is not
enough to check word lists: every sentence the runner can say, and every switch
that changes how the approach behaves, is asserted here too.  A reworded reply or
a new `--approach-*` flag fails until the doc is updated (2026-10-27: the doc was
missed when the approach was added, so the guard now covers speech as well).
"""
import importlib.util
import re
from pathlib import Path

from host.app.vision.digit_service import ReadResult, spoken
from host.app.voice import intent as intent_mod
from host.app.voice.agent import EXIT_WORDS, STOP_WORDS, WAKE_WORDS

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "voice_commands.md"
AGENT = ROOT / "tools" / "voice_agent.py"


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _runner():
    spec = importlib.util.spec_from_file_location("voice_agent_doc", AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_agent_word_lists_are_documented():
    doc = _doc_text()
    missing = [
        word
        for word in list(WAKE_WORDS) + list(STOP_WORDS) + list(EXIT_WORDS)
        if word not in doc
    ]
    assert not missing, "docs/voice_commands.md is missing: %s" % missing


def test_intent_word_lists_are_documented():
    doc = _doc_text()
    missing = [
        word
        for word in list(intent_mod._STAND_WORDS)
        + list(intent_mod._LIE_WORDS)
        + list(intent_mod._READ_WORDS)
        if word not in doc
    ]
    assert not missing, "docs/voice_commands.md is missing: %s" % missing


def test_movement_verbs_are_documented():
    doc = _doc_text()
    for verb in ("带我去", "走到", "前往", "过去", "去", "到"):
        assert verb in doc, "movement verb %r is not documented" % verb


def test_target_range_is_documented():
    doc = _doc_text()
    assert str(intent_mod.VALID_TARGET_MIN) in doc
    assert str(intent_mod.VALID_TARGET_MAX) in doc
    assert "0.6" in doc, "station spacing must be documented"


def test_stop_word_position_rule_is_documented():
    assert "句首" in _doc_text()


def test_every_spoken_sentence_is_documented():
    """The operator hears these; the doc must list them verbatim."""
    cases = [
        ReadResult(digits="3", confidence=0.99, agreement=1.0, shots=3, reason="ok"),
        ReadResult(digits="3", confidence=0.52, agreement=0.66, shots=3, reason="unclear"),
        ReadResult(digits="", confidence=0.0, agreement=0.0, shots=3, reason="empty"),
        ReadResult(digits="", confidence=0.0, agreement=0.0, shots=0, reason="unreachable"),
        ReadResult(digits="", confidence=0.0, agreement=0.0, shots=4, reason="blurred"),
    ]
    doc = _doc_text()
    missing = [line for line in (spoken(case) for case in cases) if line not in doc]
    assert not missing, "spoken sentences missing from docs/voice_commands.md: %s" % missing


def test_approach_replies_are_documented():
    """Every sentence the approach can say must be in the doc."""
    module = _runner()
    doc = _doc_text()
    missing = [
        name
        for name in ("APPROACH_ACK", "APPROACH_NO_CARD", "APPROACH_REFUSED")
        if getattr(module, name) not in doc
    ]
    assert not missing, "approach replies missing from the doc: %s" % missing


def test_every_reply_template_is_documented():
    """All REPLY_* strings, verbatim - the doc lists them template by template."""
    module = _runner()
    doc = _doc_text()
    templates = {
        name: value
        for name, value in vars(module).items()
        if name.startswith("REPLY_") and isinstance(value, str)
    }
    assert len(templates) >= 7, "did the reply constants get renamed?"
    missing = sorted(name for name, value in templates.items() if value not in doc)
    assert not missing, "reply templates missing from the doc: %s" % [
        (name, templates[name]) for name in missing
    ]


def test_approach_thresholds_are_documented():
    module = _runner()
    doc = _doc_text()
    for text in (
        "%d px" % module.APPROACH_TARGET_PX,
        "%d px" % module.APPROACH_MAX_PX,
        "%.2f m" % module.APPROACH_STEP_M,
        "%d 步" % module.APPROACH_MAX_STEPS,
    ):
        assert text in doc, "approach threshold %r is not documented" % text


def test_every_approach_flag_is_documented():
    """A new --approach-* / --dog-host switch must show up in the doc's table."""
    doc = _doc_text()
    flags = set(re.findall(r'"(--[a-z][a-z0-9-]*)"', AGENT.read_text(encoding="utf-8")))
    watched = sorted(f for f in flags if f.startswith("--approach") or f == "--dog-host")
    assert watched, "no approach flags found - did the argparse change?"
    missing = [flag for flag in watched if flag not in doc]
    assert not missing, "switches missing from the doc: %s" % missing


def test_seek_replies_are_documented():
    """The sweep-and-find sentences are operator-facing too (conventions §11)."""
    module = _runner()
    doc = _doc_text()
    names = ("SEEK_ACK", "SEEK_FOUND", "SEEK_MISS", "SEEK_NO_MOTION", "SEEK_CRASH",
             "SEEK_TURN_FAILED")
    missing = [name for name in names if getattr(module, name) not in doc]
    assert not missing, "seek replies missing from the doc: %s" % missing


def test_seek_command_and_flags_are_documented():
    """The new spoken command and its switch must be listed, not just implied."""
    doc = _doc_text()
    assert "找数字八" in doc, "the sweep-and-find example is missing from the doc"
    assert "--seek-confidence" in doc, "--seek-confidence is missing from the doc"

