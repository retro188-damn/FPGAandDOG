"""Frame grabber that runs on the perception board (USB RealSense, cv2 4.1.1).

The board is the only machine with the camera, the laptop is the only machine
with the recognition code, so this process exposes a deliberately tiny request
protocol instead of the usual RTSP stream (the RTSP server on the board allows
only one client and is occasionally busy with the vendor stack):

    client -> b"GRAB\n"
    server -> 4-byte big-endian length + JPEG bytes
    client -> b"PING\n"
    server -> b"PONG\n"

Runs on python3.6 (board image), so: no dataclasses, no walrus, no f-string
debug syntax.
"""
import argparse
import socket
import subprocess
import struct
import sys
import threading
import time

import cv2

try:
    import numpy as np
except ImportError:  # pragma: no cover - cv2 always ships numpy
    np = None


def apply_v4l2(device, exposure, gain):
    """Pin exposure/gain before the device is opened, and report what stuck.

    Why before ``VideoCapture``: changing these controls while a stream is open
    kills the running capture (verified 2026-10-28 -- the server died with a
    refused connection right after a ``--set-ctrl``).  So the values are applied
    once, at startup, and never touched again while serving.

    ``exposure=None`` means "leave the camera exactly as the driver has it";
    ``exposure=0`` explicitly restores auto exposure (``exposure_auto=3``),
    anything > 0 switches to manual (``exposure_auto=1``) and writes
    ``exposure_absolute``.  The unit is 100 us (UVC convention, confirmed by the
    readback: 166 ~= 1/60 s).

    "Untouched" is not the same as "auto": if a previous run left the camera in
    manual mode, starting without flags inherits that stale value -- that is
    exactly how the 2026-10-28 test session opened at exposure 100/median 55
    while the room had got darker.  Pass ``--exposure 0`` to force auto.
    """
    if exposure is None and gain is None:
        return "untouched"
    base = ["v4l2-ctl", "-d", "/dev/video%d" % device]
    commands = []
    if exposure is None:
        pass
    elif exposure:
        commands.append(["--set-ctrl=exposure_auto=1"])
        commands.append(["--set-ctrl=exposure_absolute=%d" % int(exposure)])
    else:
        commands.append(["--set-ctrl=exposure_auto=3"])
    if gain is not None:
        commands.append(["--set-ctrl=gain=%d" % int(gain)])
    for extra in commands:
        try:
            subprocess.call(base + extra, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        except OSError:
            return "v4l2-ctl missing (controls not applied)"
    try:
        probe = subprocess.Popen(base + ["-C", "exposure_auto,exposure_absolute,gain"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = probe.communicate()
    except OSError:
        return "v4l2-ctl missing (controls not applied)"
    return out.decode("utf-8", "replace").strip().replace("\n", " ")


class Grabber(object):
    """Open the camera once and serve frames; guarded by a lock."""

    def __init__(self, device, width, height, quality, warmup,
                 exposure=None, gain=None):
        self.device = device
        self.width = width
        self.height = height
        self.quality = quality
        self.warmup = warmup
        self.exposure = exposure
        self.gain = gain
        self.controls = ""
        self.lock = threading.Lock()
        self.capture = None
        self.opened_at = 0.0

    def _open(self):
        # Controls first: writing them while the stream is open tears it down.
        self.controls = apply_v4l2(self.device, self.exposure, self.gain)
        capture = cv2.VideoCapture(self.device)
        if not capture.isOpened():
            raise RuntimeError("cannot open /dev/video%d" % self.device)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        for _ in range(self.warmup):
            capture.read()
        self.capture = capture
        self.opened_at = time.time()

    def grab(self):
        """Return (jpeg_bytes, width, height)."""
        with self.lock:
            if self.capture is None or time.time() - self.opened_at > 600:
                if self.capture is not None:
                    self.capture.release()
                self._open()
            frame = None
            ok = False
            for _ in range(5):
                ok, frame = self.capture.read()
                if ok:
                    break
            if not ok or frame is None:
                self.capture.release()
                self.capture = None
                raise RuntimeError("frame read failed")
            ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
            if not ok:
                raise RuntimeError("jpeg encode failed")
            return buffer.tobytes(), frame.shape[1], frame.shape[0]


def serve(host, port, grabber, log):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(4)
    log("listening on %s:%d" % (host, port))
    while True:
        connection, address = server.accept()
        log("client %s:%d" % address)
        try:
            connection.settimeout(30.0)
            handle(connection, grabber, log)
        except Exception as exc:  # noqa: BLE001 - a bad client must not kill the server
            log("client error: %r" % (exc,))
        finally:
            connection.close()


def handle(connection, grabber, log):
    buffer = b""
    while True:
        try:
            data = connection.recv(64)
        except socket.timeout:
            return
        if not data:
            return
        buffer += data
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            command = line.strip().upper()
            if command == b"PING":
                connection.sendall(b"PONG\n")
                continue
            if command != b"GRAB":
                connection.sendall(b"ERR unknown\n")
                continue
            started = time.time()
            payload, width, height = grabber.grab()
            connection.sendall(struct.pack(">I", len(payload)) + payload)
            log("sent %d bytes (%dx%d) in %.0f ms" % (len(payload), width, height, (time.time() - started) * 1000))


def main():
    parser = argparse.ArgumentParser(description="camera frame server for the perception board")
    parser.add_argument("--device", type=int, default=2, help="/dev/videoN (2 = RealSense colour)")
    parser.add_argument("--port", type=int, default=43920)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--quality", type=int, default=88)
    parser.add_argument("--warmup", type=int, default=6)
    parser.add_argument("--exposure", type=int, default=None,
                        help="exposure_absolute in 100us units; 0 = force auto, "
                             "omit = leave the driver untouched")
    parser.add_argument("--gain", type=int, default=None, help="fixed gain; omit = leave camera")
    args = parser.parse_args()

    grabber = Grabber(args.device, args.width, args.height, args.quality,
                      args.warmup, args.exposure, args.gain)
    grabber._open()

    def log(message):
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), message))
        sys.stdout.flush()

    log("camera ready %dx%d" % (args.width, args.height))
    log("controls: %s" % (grabber.controls or "auto (untouched)"))
    try:
        serve(args.host, args.port, grabber, log)
    except KeyboardInterrupt:
        log("stopped")
    finally:
        if grabber.capture is not None:
            grabber.capture.release()


if __name__ == "__main__":
    main()
