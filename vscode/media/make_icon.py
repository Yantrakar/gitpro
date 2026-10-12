"""Draw the store icon: python3 media/make_icon.py media/icon.png 256 (needs Pillow).

The mark is site/favicon.svg: an orange g on the dark Yantrakar tile.
"""
import sys
from PIL import Image, ImageDraw

N = 1024                       # draw large, then downsample for smooth edges
OUT = int(sys.argv[2]) if len(sys.argv) > 2 else 256
S = N / 32                     # favicon.svg units -> pixels
W = 2.46                       # stroke width in favicon units
INK, ACCENT = (15, 18, 21, 255), (245, 158, 87, 255)   # #0F1215, #F59E57

img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, N - 1, N - 1], radius=int(6 * S), fill=INK)

def line(x0, y0, x1, y1, ext0=False, ext1=False):
    """Stroke a horizontal or vertical segment; ext adds a square cap or fills a corner."""
    h = W / 2
    xs, ys = sorted((x0, x1)), sorted((y0, y1))
    if y0 == y1:
        a, b = xs[0] - (h if (ext0 if x0 < x1 else ext1) else 0), xs[1] + (h if (ext1 if x0 < x1 else ext0) else 0)
        box = [a, y0 - h, b, y0 + h]
    else:
        a, b = ys[0] - (h if (ext0 if y0 < y1 else ext1) else 0), ys[1] + (h if (ext1 if y0 < y1 else ext0) else 0)
        box = [x0 - h, a, x0 + h, b]
    d.rectangle([v * S for v in box], fill=ACCENT)

def arc(cx, cy, r, start, end):
    R = r + W / 2              # Pillow grows the width inward from the box
    d.arc([(cx - R) * S, (cy - R) * S, (cx + R) * S, (cy + R) * S], start, end, fill=ACCENT, width=round(W * S))

# bowl: half circle on the left, square on the right
arc(15.25, 13.5, 5, 90, 270)
line(15.25, 8.5, 20.5, 8.5, ext1=True)
line(15.25, 18.5, 20.5, 18.5, ext1=True)
# stem and tail
line(20.5, 8.5, 20.5, 20.5, ext0=True)
arc(16, 20.5, 4.5, 0, 90)
line(16, 25, 11.5, 25, ext1=True)

img.resize((OUT, OUT), Image.LANCZOS).save(sys.argv[1], optimize=True)
