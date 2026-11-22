"""Generate assets/icon.ico and spm/static/icon.png (stdlib only): three stacked
cards on a blue tile."""
import struct
import zlib
from pathlib import Path

BLUE, WHITE, LIGHT, CLEAR = (59, 91, 219, 255), (255, 255, 255, 255), (186, 200, 255, 255), (0, 0, 0, 0)


def inside_round(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x < x1 and y0 <= y < y1):
        return False
    cx = min(max(x, x0 + r), x1 - 1 - r)
    cy = min(max(y, y0 + r), y1 - 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def pixel(x, y, n):
    s = n / 64
    if not inside_round(x, y, 0, 0, n, n, 12 * s):
        return CLEAR
    pick = BLUE
    for i, col in enumerate((LIGHT, LIGHT, WHITE)):  # later cards cover earlier ones
        x0, y0 = (14 + i * 6) * s, (12 + i * 9) * s
        if inside_round(x, y, x0, y0, x0 + 30 * s, y0 + 22 * s, 4 * s):
            pick = col
    return pick


def png(n):
    raw = b"".join(b"\0" + b"".join(bytes(pixel(x, y, n)) for x in range(n)) for y in range(n))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


sizes = [16, 32, 48, 64, 256]
images = [png(n) for n in sizes]
out = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
for n, img in zip(sizes, images):
    out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(img), offset)
    offset += len(img)
root = Path(__file__).resolve().parents[1]
dst = root / "assets" / "icon.ico"  # Windows shortcut icon
dst.parent.mkdir(exist_ok=True)
dst.write_bytes(out + b"".join(images))
(root / "spm" / "static" / "icon.png").write_bytes(images[-1])  # window / favicon
print(dst)
