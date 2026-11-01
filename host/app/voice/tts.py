"""Offline Chinese text-to-speech through the Windows SAPI voice.

Why SAPI: the demo laptop must not depend on the network or on model downloads
(GitHub/PyPI are unreliable on this machine), the CPU cost of speaking a short
sentence is negligible, and `Microsoft Huihui` is already installed as a
zh-CN voice.  `pyttsx3` is not needed - `win32com` is present, and a PowerShell
fallback keeps this module importable on machines without it.

Public API::

    tts = Tts()                     # picks the best installed zh-CN voice
    tts.speak("收到，前往三号位置")   # blocking playback
    tts.speak("...", async_=True)   # fire and forget, returns immediately
    tts.save_wav("...", "out.wav")  # render to a file for the demo video

Every function degrades to a no-op (with a log line) instead of raising: a
missing loudspeaker must never break the dialogue loop.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import List, Optional

LOG = logging.getLogger(__name__)

PREFERRED_VOICES = ("Huihui", "Yaoyao", "Kangkang", "Xiaoxiao")
_SAPI_VOICE_NAME = "Microsoft Huihui Desktop"


def _import_win32com():
    try:
        import win32com.client  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on the machine
        LOG.info("win32com unavailable (%s); falling back to PowerShell", exc)
        return None
    return win32com.client


def available_voices() -> List[str]:
    """Installed SAPI voice names, best effort ([] when SAPI cannot be queried)."""
    client = _import_win32com()
    if client is None:
        return []
    try:
        voice = client.Dispatch("SAPI.SpVoice")
        tokens = voice.GetVoices()
        names = []
        for index in range(tokens.Count):
            names.append(str(tokens.Item(index).GetDescription()))
        return names
    except Exception as exc:  # pragma: no cover
        LOG.info("cannot enumerate SAPI voices: %s", exc)
        return []


def pick_voice(names: List[str]) -> Optional[str]:
    """Return the first installed voice matching `PREFERRED_VOICES` order."""
    for wanted in PREFERRED_VOICES:
        for name in names:
            if wanted.lower() in name.lower():
                return name
    return names[0] if names else None


class Tts:
    """Thin wrapper over SAPI; one instance can be reused for many sentences."""

    def __init__(self, voice: Optional[str] = None, rate: int = 0) -> None:
        self._client = _import_win32com()
        self._voice = voice
        self._rate = max(-10, min(10, int(rate)))
        self._speaker = None
        if self._client is not None:
            try:
                self._speaker = self._client.Dispatch("SAPI.SpVoice")
                names = available_voices()
                chosen = self._voice or pick_voice(names)
                if chosen:
                    self._voice = chosen
                    self._speaker.Voice = self._speaker.GetVoices().Item(
                        self._index_of(names, chosen)
                    )
                self._speaker.Rate = self._rate
            except Exception as exc:  # pragma: no cover
                LOG.warning("SAPI init failed: %s", exc)
                self._speaker = None

    @staticmethod
    def _index_of(names: List[str], target: str) -> int:
        for index, name in enumerate(names):
            if name == target:
                return index
        return 0

    @property
    def ready(self) -> bool:
        return self._speaker is not None or _powershell_available()

    def speak(self, text: str, async_: bool = False) -> bool:
        """Speak `text`; returns True when the engine accepted it."""
        text = (text or "").strip()
        if not text:
            return False
        if self._speaker is not None:
            try:
                self._speaker.Speak(text, 1 if async_ else 0)
                return True
            except Exception as exc:  # pragma: no cover
                LOG.warning("SAPI speak failed (%s); trying PowerShell", exc)
        return _speak_powershell(text)

    def save_wav(self, text: str, path: str) -> bool:
        """Render `text` to a 22 kHz/16-bit/mono WAV (used for evidence/video)."""
        text = (text or "").strip()
        if not text or self._client is None:
            return False
        client = self._client
        try:
            voice = client.Dispatch("SAPI.SpVoice")
            stream = client.Dispatch("SAPI.SpFileStream")
            fmt = client.Dispatch("SAPI.SpAudioFormat")
            fmt.Type = 22  # SAFT22kHz16BitMono
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            stream.Format = fmt
            stream.Open(os.path.abspath(path), 3)  # SSFMCreateForWrite
            voice.AudioOutputStream = stream
            voice.Speak(text, 0)
            stream.Close()
            return True
        except Exception as exc:  # pragma: no cover
            LOG.warning("SAPI save_wav failed: %s", exc)
            return False


def _powershell_available() -> bool:
    return sys.platform.startswith("win")


def _speak_powershell(text: str) -> bool:  # pragma: no cover - fallback path
    """Last-resort playback through System.Speech (no COM dependency)."""
    if not _powershell_available():
        LOG.warning("no speech engine available on this platform")
        return False
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Speak([Console]::In.ReadToEnd())"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if proc.returncode != 0:
            LOG.warning("PowerShell TTS failed: %s", proc.stderr.decode("utf-8", "replace"))
            return False
        return True
    except Exception as exc:
        LOG.warning("PowerShell TTS failed: %s", exc)
        return False


if __name__ == "__main__":  # manual check: python -m host.app.voice.tts "你好"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    say = " ".join(sys.argv[1:]) or "收到，前往三号位置"
    engine = Tts()
    LOG.info("voices: %s", available_voices())
    LOG.info("speaking: %s", say)
    ok = engine.speak(say)
    LOG.info("done: %s", ok)
