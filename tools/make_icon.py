"""Write a 1024x1024 PNG app icon (dark rounded square + blue shield). Stdlib only."""
import struct
import sys
import zlib

N = 1024


def inside_rr(x, y, r=200, m=60):
    x0, y0, x1, y1 = m, m, N - m, N - m
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r); cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def inside_shield(x, y, k=1.0):
    cx, cy = N / 2, 530
    x, y = cx + (x - cx) / k, cy + (y - cy) / k
    top, w, mid, bot = 230, 300, 540, 840
    if y < top or y > bot:
        return False
    if y <= mid:
        return abs(x - cx) <= w
    return abs(x - cx) <= w * ((bot - y) / (bot - mid)) ** 0.75


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def in_check(x, y):
    return min(seg_dist(x, y, 380, 530, 480, 630), seg_dist(x, y, 480, 630, 660, 430)) <= 38


rows = []
for y in range(N):
    row = bytearray([0])
    for x in range(N):
        if inside_shield(x, y):
            if in_check(x, y):
                row += bytes((255, 255, 255, 255))
            elif inside_shield(x, y, 0.86):
                row += bytes((47, 165, 105, 255))
            else:
                row += bytes((79, 140, 255, 255))
        elif inside_rr(x, y):
            g = int(24 + 14 * y / N)
            row += bytes((g, g + 4, g + 12, 255))
        else:
            row += b"\0\0\0\0"
    rows.append(bytes(row))
raw = zlib.compress(b"".join(rows), 9)


def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)


png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
