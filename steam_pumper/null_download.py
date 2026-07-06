from __future__ import annotations

import argparse
import socket
import sys
import urllib.request


def force_ipv4() -> None:
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", default="0")
    parser.add_argument("url")
    args = parser.parse_args()

    force_ipv4()
    request = urllib.request.Request(args.url, headers={"User-Agent": f"steam-download-pumper/{args.worker_id}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
