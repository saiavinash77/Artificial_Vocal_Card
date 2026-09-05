"""Gateway-side UDP capture for AVC sensor packets.

Listens on AVC_DEST_PORT (default 7777) and validates every datagram via
services.ingest.parse_packet (structural checks + CRC16). Writes each
packet as ``<outdir>/seq_<n>.hex`` for offline replay through the pipeline:

    python -m services.pipeline out/seq_7.hex --backend demo

Usage:
    python scripts/udp_capture.py [--host 0.0.0.0] [--port 7777] [--out capture]

Stop with Ctrl-C. Requires only the repository dependencies (numpy).
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ingest import PacketError, parse_packet  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Capture AVC UDP packets")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--out", default="capture")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))
    print(f"listening on {args.host}:{args.port}, writing {outdir}/seq_*.hex")

    n_ok = n_bad = 0
    try:
        while True:
            data, addr = sock.recvfrom(64 * 1024)
            try:
                pkt = parse_packet(data)
            except PacketError as e:
                n_bad += 1
                print(f"[BAD ] {addr[0]}: {e}")
                (outdir / f"bad_{n_bad}.bin").write_bytes(data)
                continue
            n_ok += 1
            names = ",".join(f"{k}:{len(v.samples)}" for k, v in pkt.streams.items())
            print(f"[OK  ] seq={pkt.seq_no} ts={pkt.timestamp_ms} "
                  f"mask={pkt.sensor_mask:#04x} ({names})")
            (outdir / f"seq_{pkt.seq_no}.hex").write_text(data.hex())
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    print(f"\ncaptured {n_ok} good / {n_bad} bad packets -> {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
