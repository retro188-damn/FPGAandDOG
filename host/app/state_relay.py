"""UDP state relay: dog -> this board -> ROS bridge + our own consumers.

Topology on the perception board (192.168.1.103):

    dog motion host (192.168.1.120:34770)
        |  raw 0x09xx frames, 50-100 Hz
        v
    state_relay.py  :43897          <- jy_exe keeps sending here (unchanged)
        |--> 127.0.0.1:43896         vendor qnx2ros (moved from 43897, ROS topics stay alive)
        |--> 192.168.1.89:43900      dev PC (live monitoring)
        |--> 192.168.1.101:43900     ZYNQ-7020 board (safety gate input)

Why a relay instead of retargeting the dog: the dog's state target is a single
ip:port in `~/jy_exe/conf/network.toml`; a relay lets several consumers see the
same stream without touching the robot.

Python 3.6 compatible, standard library only.

Usage:
    python3 state_relay.py --listen 43897 --ros 127.0.0.1:43896 \
        --forward 192.168.1.89:43900 --forward 192.168.1.101:43900
"""
import argparse
import logging
import socket
import struct
import sys
import threading
import time

CODE_NAMES = {
    0x0901: "robot_state",
    0x0902: "joint_pos",
    0x0903: "joint_vel",
    0x0904: "status_word",
    0x0905: "handle_state",
    0x0906: "unidentified_368",
    0x10901: "unidentified_52",
}


def parse_target(text):
    host, _, port = text.rpartition(":")
    return (host, int(port))


class Relay(object):
    def __init__(self, listen_port, ros_target, forward_targets, log):
        self.log = log
        self.ros_target = ros_target
        self.forward_targets = forward_targets
        self.counts = {}
        self.frames = 0
        self.started = time.time()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", listen_port))
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.log.info("relay listening on :%d, ros=%s, forward=%s",
                      listen_port, ros_target, forward_targets)

    def run(self):
        threading.Thread(target=self._report_loop).start()
        while True:
            data, peer = self.sock.recvfrom(4096)
            self.frames += 1
            if len(data) >= 12:
                code, size, typ = struct.unpack_from("<3I", data)
                self.counts[code] = self.counts.get(code, 0) + 1
            try:
                self.out.sendto(data, self.ros_target)
                for target in self.forward_targets:
                    self.out.sendto(data, target)
            except OSError as exc:
                # A dead consumer must never stop the vendor stream.
                self.log.error("forward failed peer=%s len=%d err=%r", peer, len(data), exc)

    def _report_loop(self):
        last_frames = 0
        while True:
            time.sleep(5.0)
            delta = self.frames - last_frames
            last_frames = self.frames
            detail = ", ".join("%s(%s)=%d" % (CODE_NAMES.get(c, "?"), "0x%04X" % c, n)
                               for c, n in sorted(self.counts.items()))
            self.log.info("total=%d rate=%.1f f/s  %s", self.frames, delta / 5.0, detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=43897)
    ap.add_argument("--ros", default="127.0.0.1:43896")
    ap.add_argument("--forward", action="append", default=[])
    ap.add_argument("--log", default="state_relay.log")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [state_relay] %(message)s",
        handlers=[logging.FileHandler(args.log), logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("state_relay")
    log.info("starting: argv=%s", " ".join(sys.argv))

    targets = [parse_target(t) for t in args.forward]
    relay = Relay(args.listen, parse_target(args.ros), targets, log)
    try:
        relay.run()
    except KeyboardInterrupt:
        log.info("stopped by user after %d frames", relay.frames)


if __name__ == "__main__":
    main()
