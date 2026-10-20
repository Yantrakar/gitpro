#!/usr/bin/env python3
"""Record the README demo: real gitpro and git output, drawn into site/demo.gif.

    python3 design/demo.py [--gitpro PATH] [--out site/demo.gif]

It builds a throwaway home with the demo identity Jane Doe (a work identity for the GitHub org
employer-org, a personal default for everything else), runs gitpro setup there off screen, then
records the scenes below: the commit guard refusing a commit with the wrong email, the fix, and the
same command choosing the other identity in a personal repo. Every line shown is what the commands
printed; nothing is typed in by hand. Needs Pillow and the DejaVu Sans Mono font, and a gitpro of the
version in src/gitpro/VERSION (default: the one on PATH). gitpro.dev serves the GIF; the README
links it there.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

CONFIG = """\
[default]
name = "Jane Doe"
email = "jane@personal.dev"

[[identity]]
id = "work"
name = "Jane Doe"
email = "jane.doe@employer.com"
host = "github.com"
owners = ["employer-org"]
"""

# Each scene starts on a clear screen: (folder under the home, command); "cd DIR" only moves.
SCENES = [
    [("code/api", 'git commit -m "add readme"'),
     ("code/api", "gitpro scan --fix --yes --repo .")],
    [("code/api", 'git commit -m "add readme"'),
     ("code/api", "git log -1 --format='%an <%ae>'"),
     ("code/api", "cd ~/code/blog"),
     ("code/blog", 'git commit -m "first post"'),
     ("code/blog", "git log -1 --format='%an <%ae>'")],
]

COLS, ROWS = 92, 17
SIZE, LINE, PAD, BAR = 15, 21, 18, 30
BG, BARBG, FG, DIM = "#0d1117", "#161b22", "#e6edf3", "#8b949e"
RED, GREEN, YELLOW, BLUE = "#ff7b72", "#3fb950", "#d29922", "#79c0ff"
DOTS = ("#ff5f57", "#febc2e", "#28c840")
PROMPT_MS, TYPE_MS, AFTER_TYPE_MS, HOLD_MS, END_MS = 400, 32, 300, 1500, 4500


def demo_env(home: Path, gitpro: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITPRO_", "SSH_"))}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), GIT_CONFIG_NOSYSTEM="1",
               PATH=os.pathsep.join([str(Path(gitpro).parent), "/usr/bin", "/bin"]), LANG="C.UTF-8",
               LC_ALL="C.UTF-8", NO_COLOR="1", GIT_AUTHOR_DATE="2026-10-20T10:00:00Z",
               GIT_COMMITTER_DATE="2026-10-20T10:00:00Z")
    return env


def run(cmd: str, cwd: Path, env: dict, check: bool = False) -> str:
    r = subprocess.run(["sh", "-c", cmd], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and r.returncode:
        sys.exit(f"demo: {cmd!r} failed in {cwd}:\n{r.stdout}")
    return r.stdout


def prepare(home: Path, env: dict) -> None:
    """The machine before the recording: gitpro set up, a work repo whose own .git/config sets the
    personal email (the mistake the demo catches), and a personal repo; each has a file staged."""
    (home / ".config").mkdir(mode=0o700)
    cfg = home / ".config" / "gitpro.toml"
    cfg.write_text(CONFIG)
    cfg.chmod(0o600)
    for cmd in ['git config --global user.name "Jane Doe"', "git config --global user.email jane@personal.dev",
                "git config --global init.defaultBranch main"]:
        run(cmd, home, env, check=True)
    for name, url in [("api", "git@github.com:employer-org/api.git"), ("blog", "git@github.com:janedoe/blog.git")]:
        repo = home / "code" / name
        repo.mkdir(parents=True)
        run(f"git init -q && git remote add origin {url}", repo, env, check=True)
        (repo / ("README.md" if name == "api" else "post.md")).write_text("hello\n")
        run("git add -A", repo, env, check=True)
    run("gitpro setup -y --agent none", home, env, check=True)
    run("git config user.email jane@personal.dev", home / "code" / "api", env, check=True)


def record(home: Path, env: dict) -> list[list[tuple[str, str]]]:
    """Each scene's screen lines as (kind, text): kind 'cmd' carries 'folder\\0command'."""
    screens = []
    for scene in SCENES:
        lines = []
        for folder, cmd in scene:
            lines.append(("cmd", f"{folder}\0{cmd}"))
            if cmd.startswith("cd "):
                continue
            out = run(cmd, home / folder, env).replace(str(home), "~")
            if str(Path.home()) in out or str(ROOT) in out:
                sys.exit(f"demo: output of {cmd!r} names a local path")
            lines += [("out", t) for t in out.rstrip("\n").split("\n")]
        screens.append(lines)
    return screens


def wrap(text: str) -> list[str]:
    return [text[i:i + COLS] for i in range(0, len(text), COLS)] or [""]


def colour(text: str) -> str:
    head = text.lstrip()[:1]
    return {"✗": RED, "✓": GREEN, "→": YELLOW, "↩": DIM, "⚠": YELLOW}.get(head, FG)


class Screen:
    def __init__(self) -> None:
        self.font = ImageFont.truetype(FONT, SIZE)
        self.bold = ImageFont.truetype(FONT_BOLD, SIZE)
        self.cw = self.font.getlength("M")
        self.w = int(PAD * 2 + self.cw * COLS)
        self.h = BAR + PAD * 2 + LINE * ROWS

    def draw(self, rows: list[list[tuple[str, str, ImageFont.FreeTypeFont]]]) -> Image.Image:
        im = Image.new("RGB", (self.w, self.h), BG)
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, self.w, BAR], fill=BARBG)
        for i, c in enumerate(DOTS):
            d.ellipse([14 + i * 20, BAR // 2 - 6, 26 + i * 20, BAR // 2 + 6], fill=c)
        title = "gitpro demo · Jane Doe, one work and one personal identity"
        d.text((self.w / 2, BAR / 2), title, fill=DIM, font=self.font, anchor="mm")
        for y, row in enumerate(rows[-ROWS:]):
            x = PAD
            for text, fill, font in row:
                d.text((x, BAR + PAD + y * LINE), text, fill=fill, font=font)
                x += self.cw * len(text)
        return im


def frames(screens: list[list[tuple[str, str]]], scr: Screen) -> list[tuple[Image.Image, int]]:
    out: list[tuple[Image.Image, int]] = []
    for lines in screens:
        rows: list[list] = []
        for i, (kind, text) in enumerate(lines):
            if kind == "cmd":
                folder, cmd = text.split("\0")
                prompt = [(f"~/{folder}", BLUE, scr.font), (" $ ", DIM, scr.font)]
                rows.append(prompt)
                out.append((scr.draw(rows), PROMPT_MS))
                for n in range(1, len(cmd) + 1):
                    rows[-1] = prompt + [(cmd[:n], FG, scr.bold)]
                    out.append((scr.draw(rows), TYPE_MS))
                out[-1] = (out[-1][0], AFTER_TYPE_MS)
            else:
                rows += [[(part, colour(text), scr.font)] for part in wrap(text)]
                if i == len(lines) - 1 or lines[i + 1][0] == "cmd":
                    out.append((scr.draw(rows), HOLD_MS + 90 * min(len(rows), 12)))
        out[-1] = (out[-1][0], END_MS)
    return out


def palette() -> Image.Image:
    """The theme's colours and 7 antialiasing steps from the background to each, so no colour drifts."""
    def rgb(c: str) -> tuple[int, int, int]:
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    bg, cols = rgb(BG), [BG, BARBG, FG, DIM, RED, GREEN, YELLOW, BLUE, *DOTS]
    seen: list[tuple[int, int, int]] = []
    for c in map(rgb, cols):
        for k in range(1, 9):
            t = tuple(round(b + (x - b) * k / 8) for b, x in zip(bg, c))
            if t not in seen:
                seen.append(t)
    if rgb(BARBG) not in seen:
        seen.append(rgb(BARBG))
    pal = Image.new("P", (1, 1))
    pal.putpalette([v for t in [bg, *seen] for v in t])
    return pal


def save_gif(seq: list[tuple[Image.Image, int]], path: Path) -> None:
    pal = palette()
    imgs = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im, _ in seq]
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=[ms for _, ms in seq], loop=0,
                 optimize=False, disposal=1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gitpro", default=shutil.which("gitpro"), help="gitpro to record (default: on PATH)")
    ap.add_argument("--out", default=str(ROOT / "site" / "demo.gif"))
    a = ap.parse_args()
    if not a.gitpro:
        sys.exit("demo: no gitpro on PATH; pass --gitpro")
    a.gitpro = os.path.abspath(a.gitpro)
    want = (ROOT / "src" / "gitpro" / "VERSION").read_text().split()[0]
    have = subprocess.run([a.gitpro, "--version"], capture_output=True, text=True).stdout.split()[-1]
    if have != want:
        sys.exit(f"demo: {a.gitpro} is gitpro {have}; src/gitpro/VERSION says {want}")
    with tempfile.TemporaryDirectory(prefix="gitpro-demo-") as tmp:
        home = Path(tmp) / "jane"
        home.mkdir()
        env = demo_env(home, a.gitpro)
        prepare(home, env)
        screens = record(home, env)
    seq = frames(screens, Screen())
    save_gif(seq, Path(a.out))
    print(f"{a.out}: {len(seq)} frames, {sum(ms for _, ms in seq) / 1000:.1f} s, "
          f"{Path(a.out).stat().st_size // 1024} KiB")
    for lines in screens:
        for kind, text in lines:
            print("$ " + text.split("\0")[1] if kind == "cmd" else "  " + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
