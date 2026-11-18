#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 vnts-nas 图标：深蓝圆角底 + 青绿中心服务端节点 + 辐射卫星节点。纯标准库。"""

import math
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
# 输出目录可用第一个参数覆盖：Docker 镜像里 /app 的目录布局与仓库不同，
# 靠相对路径推不出来，必须显式指定。
OUT = (os.path.abspath(sys.argv[1]) if len(sys.argv) > 1
       else os.path.join(HERE, "..", "app", "ui", "images"))


def draw(size):
    bg_top, bg_bot = (20, 32, 58), (12, 20, 38)
    accent, accent2 = (52, 211, 178), (96, 165, 250)
    px = [[(0, 0, 0, 0) for _ in range(size)] for _ in range(size)]
    cx = cy = (size - 1) / 2.0

    def setp(x, y, color, a=1.0):
        x, y = int(x), int(y)
        if not (0 <= x < size and 0 <= y < size):
            return
        old = px[y][x]
        if old[3] == 0:
            px[y][x] = (color[0], color[1], color[2], int(255 * a))
        else:
            na = min(255, old[3] + int(255 * a))
            px[y][x] = (color[0], color[1], color[2], na)

    radius = size * 0.22
    for y in range(size):
        for x in range(size):
            dx = max(radius - x, x - (size - 1 - radius), 0)
            dy = max(radius - y, y - (size - 1 - radius), 0)
            if dx * dx + dy * dy <= radius * radius:
                t = y / max(1, size - 1)
                px[y][x] = (int(bg_top[0] + (bg_bot[0] - bg_top[0]) * t),
                            int(bg_top[1] + (bg_bot[1] - bg_top[1]) * t),
                            int(bg_top[2] + (bg_bot[2] - bg_top[2]) * t), 255)

    hub = size * 0.115
    for y in range(int(cy - hub - 2), int(cy + hub + 3)):
        for x in range(int(cx - hub - 2), int(cx + hub + 3)):
            if ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 <= hub:
                setp(x, y, accent, 1.0)

    ring, sat_r, n = size * 0.30, size * 0.052, 6
    for i in range(n):
        ang = (math.pi * 2 / n) * i - math.pi / 2
        sx, sy = cx + ring * math.cos(ang), cy + ring * math.sin(ang)
        steps = max(4, int(ring * 2))
        for s in range(steps):
            t = s / steps
            setp(cx + (sx - cx) * t, cy + (sy - cy) * t, accent2, 0.45)
        for y in range(int(sy - sat_r - 2), int(sy + sat_r + 3)):
            for x in range(int(sx - sat_r - 2), int(sx + sat_r + 3)):
                if ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5 <= sat_r:
                    setp(x, y, accent2, 1.0)
    return px


def write_png(path, px):
    size = len(px)
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw += bytes(px[y][x])

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def main():
    os.makedirs(OUT, exist_ok=True)
    for size in (64, 256):
        write_png(os.path.join(OUT, "icon_%d.png" % size), draw(size))
        print("生成 icon_%d.png" % size)


if __name__ == "__main__":
    main()
