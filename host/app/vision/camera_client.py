"""PC-side client for the perception board's frame grabber.

    from vision.camera_client import grab_frame
    image = grab_frame("192.168.1.103", 43920)

The protocol is one newline command, one length-prefixed JPEG back - see
``capture_server.py`` for the wire format.
"""
from __future__ import annotations

import io
import socket
import struct
import time
from typing import Tuple

from PIL import Image

CAMERA_HOST = "192.168.1.103"
CAMERA_PORT = 43920


class CameraError(RuntimeError):
    """Raised when the board cannot deliver a frame."""


def grab_frame(host: str = CAMERA_HOST, port: int = CAMERA_PORT,
               timeout_s: float = 8.0) -> Image.Image:
    """Request one frame and decode it; raises CameraError on failure."""
    started = time.time()
    with socket.create_connection((host, port), timeout=timeout_s) as connection:
        connection.settimeout(timeout_s)
        connection.sendall(b"GRAB\n")
        header = b""
        while len(header) < 4:
            chunk = connection.recv(4 - len(header))
            if not chunk:
                raise CameraError("connection closed while reading length")
            header += chunk
        size = struct.unpack(">I", header)[0]
        if size == 0 or size > 20 * 1024 * 1024:
            raise CameraError(f"implausible frame size {size}")
        payload = b""
        while len(payload) < size:
            chunk = connection.recv(min(65536, size - len(payload)))
            if not chunk:
                raise CameraError(f"connection closed at {len(payload)}/{size} bytes")
            payload += chunk
    image = Image.open(io.BytesIO(payload))
    image.load()
    image.info["transfer_ms"] = (time.time() - started) * 1000.0
    return image


def grab_frame_raw(host: str = CAMERA_HOST, port: int = CAMERA_PORT,
                   timeout_s: float = 8.0) -> Tuple[Image.Image, float]:
    """Return (image, milliseconds spent on the request)."""
    image = grab_frame(host, port, timeout_s)
    return image, float(image.info.get("transfer_ms", 0.0))


def ping(host: str = CAMERA_HOST, port: int = CAMERA_PORT, timeout_s: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as connection:
            connection.sendall(b"PING\n")
            return connection.recv(16).startswith(b"PONG")
    except OSError:
        return False


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="grab one frame from the board camera")
    parser.add_argument("--host", default=CAMERA_HOST)
    parser.add_argument("--port", type=int, default=CAMERA_PORT)
    parser.add_argument("--out", default="_scratch/frame.jpg")
    args = parser.parse_args()
    image, elapsed = grab_frame_raw(args.host, args.port)
    image.save(args.out)
    print(f"{image.width}x{image.height} in {elapsed:.0f} ms -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
