"""Draw the store icon: python3 media/make_icon.py media/icon.png 256 (needs Pillow)."""
import sys
from PIL import Image, ImageDraw

N = 1024                       # draw large, then downsample for smooth edges
OUT = int(sys.argv[2]) if len(sys.argv) > 2 else 256

def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))

top, bottom = (14, 165, 233), (79, 70, 229)           # sky-500 -> indigo-600
grad = Image.new("RGBA", (N, N))
gd = ImageDraw.Draw(grad)
for y in range(N):
    gd.line([(0, y), (N, y)], fill=lerp(top, bottom, y / (N - 1)) + (255,))
mask = Image.new("L", (N, N), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, N - 1, N - 1], radius=int(N * 0.22), fill=255)
img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)

person = Image.new("L", (N, N), 0)
pd = ImageDraw.Draw(person)
cx = N * 0.44
pd.ellipse([cx - 150, 200, cx + 150, 500], fill=255)                  # head
pd.ellipse([cx - 290, 560, cx + 290, 1140], fill=255)                 # shoulders
person = Image.composite(person, Image.new("L", (N, N), 0), mask)     # clip to the tile
img.paste((255, 255, 255, 255), (0, 0), person)

d = ImageDraw.Draw(img)
bx, by, r = N * 0.745, N * 0.735, 175                                 # check badge
d.ellipse([bx - r - 34, by - r - 34, bx + r + 34, by + r + 34], fill=bottom + (255,))
d.ellipse([bx - r, by - r, bx + r, by + r], fill=(34, 197, 94, 255))  # green-500
d.line([(bx - 85, by + 5), (bx - 22, by + 70), (bx + 92, by - 62)], fill=(255, 255, 255, 255),
       width=52, joint="curve")
for x, y in [(bx - 85, by + 5), (bx + 92, by - 62)]:
    d.ellipse([x - 26, y - 26, x + 26, y + 26], fill=(255, 255, 255, 255))

img.resize((OUT, OUT), Image.LANCZOS).save(sys.argv[1], optimize=True)
