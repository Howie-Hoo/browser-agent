#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""残留进程清理入口（console script browser-agent-cleanup）。清理逻辑见同目录 cleanup.sh。"""
import os
import subprocess
import sys


def main() -> int:
    if sys.platform == "win32":
        print("[跳过] Windows 下请手动结束 python / Chrome 进程", file=sys.stderr)
        return 0
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cleanup.sh")
    subprocess.run(
        ["bash", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
