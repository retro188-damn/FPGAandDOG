"""Best-effort microphone capture (optional dependency: `sounddevice`).

`sounddevice` is installed on the dev PC (0.5.6) but is deliberately optional:
importing this module stays safe, and only the capture functions raise, with
one actionable English line::

    RuntimeError: microphone capture requires the 'sounddevice' package: pip install sounddevice

Dev-PC note (2026-10-25): the built-in array is a 2-channel device.  Opening it
with `channels=1` returns broken data (shared mode gives exact zeros), which is
why capture here must ask for 2 channels and downmix; `device=9` +
`channels=2` + shared mode is the configuration that works.

Both functions return mono float32 in [-1, 1] and neither needs a second
dependency: the endpoint detector is a plain RMS threshold in numpy.
"""
import collections
import logging
from typing import List, Optional

import numpy as np

LOG = logging.getLogger(__name__)

try:  # pragma: no cover - depends on the machine
    import sounddevice as _sd
except Exception as exc:  # ImportError, or OSError when the PortAudio DLL is missing
    _sd = None  # type: ignore[assignment]
    _SD_IMPORT_ERROR = exc  # type: Optional[Exception]
else:
    _SD_IMPORT_ERROR = None

_SOUNDDEVICE_HINT = (
    "microphone capture requires the 'sounddevice' package: pip install sounddevice"
)


def is_available() -> bool:
    """True when a usable `sounddevice` import exists (no device probe is done)."""
    return _sd is not None


def _require_sounddevice():
    if _sd is None:
        raise RuntimeError(
            "%s (import failed: %s)" % (_SOUNDDEVICE_HINT, _SD_IMPORT_ERROR)
        )
    return _sd


def stream_kwargs(
    device: Optional[int] = None, exclusive: bool = False
) -> dict:
    """Build the extra settings for a capture stream.

    Dev-PC finding (2026-10-25): the capture failure was NOT about shared vs
    exclusive but about the channel count (this array needs `channels=2`).
    Exclusive mode stays available as a debugging option only.
    """
    extra = None
    if exclusive:
        extra = _sd.WasapiSettings(exclusive=True)  # type: ignore[union-attr]
    return {"device": device, "extra_settings": extra}


def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resample; adequate for 16 kHz ASR features."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    n_dst = int(round(audio.size * float(dst_rate) / float(src_rate)))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.float32)
    x_src = np.arange(audio.size, dtype=np.float64) / float(src_rate)
    x_dst = np.arange(n_dst, dtype=np.float64) / float(dst_rate)
    return np.interp(x_dst, x_src, audio).astype(np.float32)


def _open_rate(sd, device, requested_rate: int, exclusive: bool,
               channels: int = 1) -> int:
    """Pick a rate the endpoint actually accepts, preferring `requested_rate`.

    The dev-PC array rejects 16 kHz when opened as 2 channels (`PaErrorCode
    -9997`, Invalid sample rate), so probe the requested rate first and fall
    back to the endpoint native rate, letting the caller resample.
    """
    dev = device if device is not None else sd.default.device[0]
    try:
        sd.check_input_settings(
            device=dev, samplerate=requested_rate, channels=int(channels),
            dtype="float32",
        )
        return requested_rate
    except Exception:
        pass
    info = sd.query_devices(dev)
    native = int(round(info["default_samplerate"]))
    if native > 0:
        try:
            sd.check_input_settings(
                device=dev, samplerate=native, channels=int(channels),
                dtype="float32",
            )
            return native
        except Exception:
            pass
    return requested_rate


def _apply_gain(audio: np.ndarray, gain: float) -> np.ndarray:
    """Scale and clip; exclusive-mode capture is ~-55 dBFS on this laptop."""
    if gain == 1.0:
        return audio
    return np.clip(audio * float(gain), -1.0, 1.0)


def _downmix(data: np.ndarray, channels: int) -> np.ndarray:
    """Flatten an interleaved capture to mono.

    Dev-PC finding (2026-10-25): this 2-channel array delivers a much quieter,
    partially decorrelated signal when asked for `channels=1` in exclusive
    mode; capturing both channels and averaging them is the working path.
    """
    audio = np.asarray(data, dtype=np.float32)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return np.clip(audio.reshape(-1), -1.0, 1.0)


def record(
    seconds: float,
    sample_rate: int = 16000,
    device: Optional[int] = None,
    exclusive: bool = False,
    gain: float = 1.0,
    channels: int = 1,
) -> np.ndarray:
    """Capture exactly `seconds` of mono audio at `sample_rate`, float32 in [-1, 1]."""
    sd = _require_sounddevice()
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError("sample_rate must be positive, got %d" % rate)
    frames = int(round(float(seconds) * rate))
    if frames <= 0:
        raise ValueError("seconds must be > 0, got %r" % (seconds,))

    LOG.info("recording %.2fs at %d Hz", frames / float(rate), rate)
    kwargs = stream_kwargs(device, exclusive)
    open_rate = _open_rate(sd, device, rate, exclusive, int(channels))
    frames_open = int(round(frames * float(open_rate) / float(rate)))
    data = sd.rec(
        frames_open, samplerate=open_rate, channels=int(channels), dtype="float32",
        blocking=True, **kwargs
    )
    audio = _downmix(data, int(channels))
    audio = _resample(audio, open_rate, rate)
    return _apply_gain(np.clip(audio, -1.0, 1.0), gain)


def auto_threshold(floor_rms: float, floor: float = 3.0, minimum: float = 0.003) -> float:
    """Speech threshold derived from the measured noise floor.

    The dev-PC array's level swings by more than 20x between recordings (0.011
    to 0.32 peak), so a fixed threshold either misses quiet speech or trips on
    noise.  `floor` is the multiplier over the measured floor; `minimum` keeps
    a dead-quiet room from producing an absurdly low trigger.
    """
    return max(float(floor_rms) * float(floor), float(minimum))


def record_with_vad_endpoint(
    max_seconds: float = 8.0,
    extend_seconds: float = 4.0,
    silence_ms: int = 700,
    sample_rate: int = 16000,
    block_ms: int = 100,
    rms_threshold: Optional[float] = 0.015,
    device: Optional[int] = None,
    exclusive: bool = False,
    gain: float = 1.0,
    channels: int = 1,
    stop_flag=None,
) -> np.ndarray:
    """Capture until the speaker pauses (~`silence_ms` below the RMS threshold).

    The stream is read in `block_ms` chunks; a chunk whose RMS is at or above
    `rms_threshold` marks speech, and the recording stops after `silence_ms` of
    consecutive quiet chunks.  A 200 ms pre-roll is prepended so the first
    syllable is not clipped.  Returns float32 in [-1, 1]; returns an EMPTY array
    when no speech crossed the threshold within `max_seconds`.

    Real-run finding (2026-10-26): a sentence that starts near the end of the
    `max_seconds` window was cut mid-word, because the block budget ran out
    while the speaker was still talking.  When that happens the capture now
    keeps going until the speaker pauses, bounded by `extend_seconds`.
    """
    sd = _require_sounddevice()
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError("sample_rate must be positive, got %d" % rate)
    if max_seconds <= 0:
        raise ValueError("max_seconds must be > 0, got %r" % (max_seconds,))
    if extend_seconds < 0:
        raise ValueError("extend_seconds must be >= 0, got %r" % (extend_seconds,))
    if block_ms <= 0 or silence_ms <= 0:
        raise ValueError("block_ms and silence_ms must be > 0")

    block = max(1, int(rate * block_ms / 1000.0))
    max_blocks = max(1, int(max_seconds * 1000.0 / block_ms))
    silence_blocks = max(1, int(round(silence_ms / float(block_ms))))
    preroll_blocks = max(1, int(round(200.0 / block_ms)))

    preroll = collections.deque(maxlen=preroll_blocks)  # type: collections.deque
    chunks = []  # type: List[np.ndarray]
    speaking = False
    quiet = 0
    LOG.info("listening (max %.1fs, stop after %d ms silence)", max_seconds, silence_ms)

    open_rate = _open_rate(sd, device, rate, exclusive, int(channels))
    block = max(1, int(open_rate * block_ms / 1000.0))
    block_ms_actual = 1000.0 * block / float(open_rate)
    max_blocks = max(1, int(max_seconds * 1000.0 / block_ms_actual))
    silence_blocks = max(1, int(round(silence_ms / block_ms_actual)))
    preroll_blocks = max(1, int(round(200.0 / block_ms_actual)))
    calib_blocks = max(1, int(round(500.0 / block_ms_actual)))
    extend_blocks = max(0, int(round(float(extend_seconds) * 1000.0 / block_ms_actual)))
    with sd.InputStream(
        samplerate=open_rate, channels=int(channels), dtype="float32", blocksize=block,
        **stream_kwargs(device, exclusive)
    ) as stream:
        threshold = rms_threshold
        if threshold is None:
            # Calibrate on the first `calib_blocks` of the recording: the room
            # floor decides the trigger, so quiet speech still registers.
            floors = []
            for _ in range(calib_blocks):
                data, _overflowed = stream.read(block)
                chunk = _downmix(data, int(channels))
                floors.append(float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0)
                preroll.append(chunk)
            floor_rms = float(np.median(floors)) if floors else 0.0
            threshold = auto_threshold(floor_rms)
            LOG.info(
                "calibrated floor %.5f -> speech threshold %.5f", floor_rms, threshold
            )
        for _ in range(max_blocks):
            if stop_flag is not None and stop_flag():
                LOG.debug("capture aborted by stop_flag")
                break
            data, _overflowed = stream.read(block)
            chunk = _downmix(data, int(channels))
            rms = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0

            if not speaking:
                # Keep a short rolling pre-roll so the onset is not cut off.
                preroll.append(chunk)
                if rms >= threshold:
                    speaking = True
                    quiet = 0
                    chunks.extend(preroll)
                    preroll.clear()
                continue

            chunks.append(chunk)
            if rms >= threshold:
                quiet = 0
                continue
            quiet += 1
            if quiet >= silence_blocks:
                LOG.info(
                    "endpoint detected after %.2fs",
                    len(chunks) * block_ms_actual / 1000.0,
                )
                break

        if speaking and quiet < silence_blocks and extend_blocks > 0:
            # The block budget ran out while the speaker was still talking:
            # keep capturing instead of returning a sentence cut in half.
            LOG.info(
                "window full (%.1fs) but still speaking -> extending up to %.1fs",
                max_seconds, extend_seconds,
            )
            for _ in range(extend_blocks):
                if stop_flag is not None and stop_flag():
                    LOG.debug("capture aborted by stop_flag")
                    break
                data, _overflowed = stream.read(block)
                chunk = _downmix(data, int(channels))
                rms = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0
                chunks.append(chunk)
                if rms >= threshold:
                    quiet = 0
                    continue
                quiet += 1
                if quiet >= silence_blocks:
                    LOG.info(
                        "endpoint detected after %.2fs",
                        len(chunks) * block_ms_actual / 1000.0,
                    )
                    break

    if not chunks:
        # Idle windows are normal (the agent listens all the time); logging them as
        # warnings filled the demo console with things that looked like failures.
        LOG.debug(
            "no speech above rms %.4f within %.1fs", threshold, max_seconds
        )
        return np.zeros(0, dtype=np.float32)
    audio = _resample(np.concatenate(chunks), open_rate, rate)
    return _apply_gain(np.clip(audio, -1.0, 1.0), gain)
