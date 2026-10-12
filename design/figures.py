#!/usr/bin/env python3
"""Draw the figures on gitpro.dev and check their geometry.

Each figure has a wide variant and a narrow one (about 320 units, for phones). The SVG goes into
index.html between <!-- fig:NAME --> and <!-- /fig:NAME -->, and the routes the packets travel go
into data-routes for site.js. Run it after changing a figure: python3 design/figures.py

It exits 1 when a text would leave its box, two texts overlap, or a route crosses a text or a box.
Text widths are estimates (sans 0.56 em, mono 0.6 em per character), so look at the result too.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "site" / "index.html"

# (font size, em per character, letter spacing in em) per class, wide and narrow; narrow follows svg.nv in styles.css
FONTS = {
    "w": {"lbl": (14, .56, 0), "lbl-c": (13, .56, 0), "sub": (12, .6, .02), "sub-ink": (12, .6, .02), "lbl-mono": (11, .6, .08), "st": (11, .6, 0)},
    "n": {"lbl": (13, .56, 0), "lbl-c": (13, .56, 0), "sub": (12, .6, .02), "sub-ink": (12, .6, .02), "lbl-mono": (12, .6, .04), "st": (12, .6, 0)},
}
PAD = 8          # text to box edge
PACKET = 4       # packet radius: routes keep this far from text


class Fig:
    def __init__(self, name: str, v: str, w: int, h: int, title: str, desc: str):
        self.name, self.v, self.w, self.h, self.title, self.desc = name, v, w, h, title, desc
        self.out: list[str] = []
        self.texts: list[tuple] = []        # (x0, y0, x1, y1, text, box key or None)
        self.boxes: dict[str, tuple] = {}   # key -> (x, y, w, h, is_container)
        self.routes: dict[str, list] = {}
        self.segs: list[tuple] = []         # drawn and undrawn route segments, for the checks
        self.id = f"{name}{v}"

    # ---- drawing
    def _width(self, s: str, cls: str) -> float:
        size, em, ls = FONTS[self.v][cls.split()[0]]
        return len(s) * size * (em + ls)

    def _text(self, x, y, s, cls, anchor="start", k=None, inside=None, alts=()):
        size = FONTS[self.v][cls.split()[0]][0]
        wide = max(self._width(t, cls) for t in (s, *alts))
        x0 = {"start": x, "middle": x - wide / 2, "end": x - wide}[anchor]
        self.texts.append((x0, y - .75 * size, x0 + wide, y + .22 * size, s or alts[0], inside))
        attrs = f' data-k="{k}"' if k else ""
        anchor_attr = "" if anchor == "start" else f' text-anchor="{anchor}"'
        self.out.append(f'<text class="{cls}"{attrs} x="{x:g}" y="{y:g}"{anchor_attr}>{html.escape(s)}</text>')

    def label(self, x, y, s, cls="lbl-mono", anchor="start", k=None, alts=()):
        self._text(x, y, s, cls, anchor, k, None, alts)

    def status(self, x, y, alts):
        self._text(x, y, "", "st", "end", "st", None, alts)

    def box(self, k, x, y, w, h, lines, cls="box", container=False):
        """lines: (text, class) or (text, class, key, alternatives) for text site.js changes."""
        self.boxes[k] = (x, y, w, h, container)
        self.out.append(f'<rect class="{cls}" data-k="{k}" x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="2"/>')
        if container:
            return
        heights = [FONTS[self.v][ln[1].split()[0]][0] * 1.3 for ln in lines]
        top = y + (h - sum(heights)) / 2
        for ln, lh in zip(lines, heights):
            text, cls_t = ln[0], ln[1]
            key, alts = (ln[2], ln[3]) if len(ln) > 2 else (None, ())
            self._text(x + w / 2, round(top + lh * .78, 1), text, cls_t, "middle", key, k, alts)
            top += lh

    def band(self, k, x, y, w, h, title, cls="band"):
        self.box(k, x, y, w, h, [], cls, container=True)
        if title:
            self.label(x + 14, y + 22, title, "lbl-mono acc" if cls == "band-focus" else "lbl-mono")

    def route(self, k, pts, draw=True, arrow=True, cls="ln"):
        self.routes[k] = [[round(px, 1), round(py, 1)] for px, py in pts]
        self.segs += [(k, a, b) for a, b in zip(pts, pts[1:])]
        if draw:
            d = "M" + " L".join(f"{px:g} {py:g}" for px, py in pts)
            m = f' marker-end="url(#{self.id}-m)"' if arrow else ""
            self.out.append(f'<path class="{cls}" d="{d}"{m}/>')

    def xmark(self, cx, cy, text, anchor="start"):
        self.out.append(f'<g class="xm" data-k="x-mark"><line class="ln-accent" x1="{cx - 5:g}" y1="{cy - 5:g}" '
                        f'x2="{cx + 5:g}" y2="{cy + 5:g}"/><line class="ln-accent" x1="{cx - 5:g}" y1="{cy + 5:g}" '
                        f'x2="{cx + 5:g}" y2="{cy - 5:g}"/></g>')
        self.texts.append((cx - 6, cy - 6, cx + 6, cy + 6, "×", None))
        tx = cx + 14 if anchor == "start" else cx - 14
        self.label(tx, cy + 4, text, "lbl-mono acc-raw", anchor)

    # ---- checks
    def check(self) -> list[str]:
        errs = []
        for x0, y0, x1, y1, s, k in self.texts:
            if x0 < 0 or y0 < 0 or x1 > self.w or y1 > self.h:
                errs.append(f"text {s!r} leaves the drawing")
            if k:
                bx, by, bw, bh, _ = self.boxes[k]
                if x0 < bx + PAD or x1 > bx + bw - PAD or y0 < by + 4 or y1 > by + bh - 4:
                    errs.append(f"text {s!r} does not fit box {k} ({x1 - x0:.0f} wide, room {bw - 2 * PAD})")
            for kk, (bx, by, bw, bh, cont) in self.boxes.items():
                if kk != k and not cont and x0 < bx + bw and x1 > bx and y0 < by + bh and y1 > by:
                    errs.append(f"text {s!r} overlaps box {kk}")
        for i, a in enumerate(self.texts):
            for b in self.texts[i + 1:]:
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    errs.append(f"texts {a[4]!r} and {b[4]!r} overlap")
        for k, (ax, ay), (bx, by) in self.segs:
            if ax != bx and ay != by:
                errs.append(f"route {k} has a diagonal segment")
            sx0, sx1, sy0, sy1 = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
            for x0, y0, x1, y1, s, _ in self.texts:
                if sx0 - PACKET < x1 and sx1 + PACKET > x0 and sy0 - PACKET < y1 and sy1 + PACKET > y0:
                    errs.append(f"route {k} passes over text {s!r}")
            for kk, (bx_, by_, bw, bh, cont) in self.boxes.items():
                if not cont and sx0 < bx_ + bw - 1 and sx1 > bx_ + 1 and sy0 < by_ + bh - 1 and sy1 > by_ + 1:
                    errs.append(f"route {k} crosses box {kk}")
        for i, (ka, a) in enumerate(self.boxes.items()):
            for kb, b in list(self.boxes.items())[i + 1:]:
                if a[4] or b[4]:
                    continue
                if a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]:
                    errs.append(f"boxes {ka} and {kb} overlap")
        return [f"{self.id}: {e}" for e in errs]

    def svg(self) -> str:
        cls = f"{self.id} {'wv' if self.v == 'w' else 'nv'} sw"
        marker = (f'<defs><marker id="{self.id}-m" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" '
                  f'markerHeight="6" markerUnits="userSpaceOnUse" orient="auto"><path class="arrow" '
                  f'd="M0 0L10 5L0 10z"/></marker></defs>')
        routes = html.escape(json.dumps(self.routes, separators=(",", ":")), quote=False)
        return "\n".join([
            f'<svg class="{cls}" data-fig="{self.name}" data-v="{self.v}" viewBox="0 0 {self.w} {self.h}" '
            f'role="img" aria-labelledby="{self.id}-t {self.id}-d" data-routes=\'{routes}\' '
            f'xmlns="http://www.w3.org/2000/svg">',
            f'<title id="{self.id}-t">{html.escape(self.title)}</title>',
            f'<desc id="{self.id}-d">{html.escape(self.desc)}</desc>',
            marker, *self.out, "</svg>"])


def hline(a, b, y):
    return [(a, y), (b, y)]


# ---------------------------------------------------------------- FIG. 01: one email for every repo

F01_T = "One email for every repository"
F01_D = ("Without gitpro, one laptop has a single user.email in ~/.gitconfig, jane@personal.dev. Two repositories "
         "on the laptop, a work repository employer-org/app and a personal one jane/dotfiles, both commit with it. "
         "The personal account on GitHub gets the right address; the employer's organisation on GitHub gets "
         "the personal address in its history.")
F01_ST = ("PERSONAL REPO", "WORK REPO, SAME EMAIL", "PERSONAL EMAIL IN WORK HISTORY")
NO_COMMITS = ("no commits yet", "jane@personal.dev")


def f01(v):
    if v == "w":
        f = Fig("f01", v, 600, 304, F01_T, F01_D)
        f.status(584, 22, F01_ST)
        f.band("laptop", 16, 36, 300, 256, "THIS LAPTOP")
        f.box("gc", 56, 74, 244, 52, [("~/.gitconfig", "lbl"), ("user.email jane@personal.dev", "sub")], "box-accent")
        f.box("r1", 56, 156, 244, 52, [("employer-org/app", "lbl"), ("work repository", "sub")])
        f.box("r2", 56, 228, 244, 52, [("jane/dotfiles", "lbl"), ("personal repository", "sub")])
        f.route("c-r1", [(56, 100), (36, 100), (36, 182), (56, 182)])
        f.route("c-r2", [(56, 100), (36, 100), (36, 254), (56, 254)])
        for i, (y, name) in enumerate([(156, "employer-org on GitHub"), (228, "jane on GitHub")], 1):
            f.box(f"m{i}", 372, y, 212, 52, [(name, "lbl"), (NO_COMMITS[0], "sub-ink", f"m{i}-v", NO_COMMITS)])
            f.route(f"p-m{i}", hline(300, 372, y + 26))
            f.label(336, y + 16, "PUSH", anchor="middle")
        return f
    f = Fig("f01", v, 320, 334, F01_T, F01_D)
    f.status(316, 16, F01_ST)
    f.band("laptop", 2, 28, 316, 186, "THIS LAPTOP")
    f.box("gc", 10, 62, 300, 52, [("~/.gitconfig", "lbl"), ("user.email jane@personal.dev", "sub")], "box-accent")
    f.box("r1", 10, 150, 146, 52, [("employer-org/app", "lbl"), ("work", "sub")])
    f.box("r2", 164, 150, 146, 52, [("jane/dotfiles", "lbl"), ("personal", "sub")])
    f.route("c-r1", [(83, 114), (83, 150)])
    f.route("c-r2", [(237, 114), (237, 150)])
    for i, (x, name) in enumerate([(10, "employer-org"), (164, "jane")], 1):
        f.box(f"m{i}", x, 254, 146, 70, [(name, "lbl"), ("on GitHub", "sub"),
                                          (NO_COMMITS[0], "sub-ink", f"m{i}-v", NO_COMMITS)])
        f.route(f"p-m{i}", [(x + 73, 202), (x + 73, 254)])
    f.label(91, 232, "PUSH")
    return f


# ---------------------------------------------------------------- FIG. 02: matched by remote URL

F02_T = "Matched by remote URL"
F02_D = ("Three repositories, employer-org/app on github.com, platform/api on gitlab.employer.com and "
         "jane/dotfiles on github.com, are each matched by their remote URL to one identity: work-github, "
         "work-gitlab or the default. Below, a user.email override in the work repository's .git/config would "
         "commit as the wrong person; the gitpro guard hook stops that commit.")
F02_ST = ("MATCH: github.com / employer-org", "MATCH: gitlab.employer.com", "NO MATCH: default identity",
          "OVERRIDE IN .git/config", "COMMIT BLOCKED, FIX SHOWN")


def f02(v):
    if v == "w":
        f = Fig("f02", v, 680, 362, F02_T, F02_D)
        f.status(664, 20, F02_ST)
        f.label(32, 48, "REPOSITORY")
        f.label(248, 48, "REMOTE URL MATCHES")
        f.label(488, 48, "IDENTITY")
        f.band("pats", 236, 58, 228, 220, "")
        repos = [("employer-org/app", "github.com"), ("platform/api", "gitlab.employer.com"),
                 ("jane/dotfiles", "github.com")]
        pats = ["github.com / employer-org", "gitlab.employer.com", "anything else"]
        ids = [("work-github", "jane.doe@employer.com"), ("work-gitlab", "jane.doe@employer.com"),
               ("default", "jane@personal.dev")]
        for i in range(3):
            y = 64 + i * 72
            cy = y + 26
            f.box(f"r{i + 1}", 32, y, 180, 52, [(repos[i][0], "lbl"), (repos[i][1], "sub")])
            f.box(f"p{i + 1}", 244, y + 6, 212, 40, [(pats[i], "lbl-c")], "chip")
            f.box(f"id{i + 1}", 488, y, 176, 52, [(ids[i][0], "lbl"), (ids[i][1], "sub")])
            f.route(f"a{i + 1}", hline(212, 244, cy))
            f.route(f"b{i + 1}", hline(456, 488, cy))
        f.box("ov", 32, 298, 180, 52, [("user.email override", "lbl"), ("in .git/config", "sub")])
        f.box("guard", 244, 298, 212, 52, [("gitpro guard", "lbl"), ("pre-commit hook", "sub")], "box-accent")
        f.route("c-ov", [(32, 90), (18, 90), (18, 324), (32, 324)])
        f.route("c-g", hline(212, 244, 324))
        f.route("c-x", hline(456, 486, 324), arrow=False)
        f.xmark(496, 324, "COMMIT BLOCKED")
        return f
    f = Fig("f02", v, 320, 394, F02_T, F02_D)
    f.status(316, 16, F02_ST)
    f.label(16, 46, "REPOSITORY")
    f.label(184, 46, "IDENTITY")
    repos = [("employer-org/app", "github.com"), ("platform/api", "work GitLab"), ("jane/dotfiles", "github.com")]
    ids = [("work-github", "work email"), ("work-gitlab", "work email"), ("default", "personal email")]
    for i in range(3):
        y = 58 + i * 68
        f.box(f"r{i + 1}", 16, y, 136, 52, [(repos[i][0], "lbl"), (repos[i][1], "sub")])
        f.box(f"id{i + 1}", 184, y, 128, 52, [(ids[i][0], "lbl"), (ids[i][1], "sub")])
        f.route(f"a{i + 1}", hline(152, 184, y + 26))
    f.box("ov", 16, 270, 136, 52, [("override", "lbl"), (".git/config", "sub")])
    f.box("guard", 184, 270, 128, 52, [("gitpro guard", "lbl"), ("pre-commit", "sub")], "box-accent")
    f.route("c-ov", [(16, 84), (6, 84), (6, 296), (16, 296)])
    f.route("c-g", hline(152, 184, 296))
    f.route("c-x", [(248, 322), (248, 352)], arrow=False)
    f.xmark(248, 364, "COMMIT BLOCKED", "end")
    return f


# ---------------------------------------------------------------- FIG. 03: an agent tries to commit

F03_T = "An AI agent tries to commit"
F03_D = ("An AI coding agent such as Claude Code runs git commit. The gitpro agent-check hook sees the wrong "
         "identity and denies the call, and the reason goes back to the agent. The agent asks you, you approve "
         "the fix, and the same commit then goes through to git as the right identity.")
F03_ST = ("AGENT RUNS git commit", "WRONG IDENTITY", "DENIED: ASK THE USER", "AGENT ASKS YOU",
          "YOU APPROVE THE FIX", "COMMITS AS work-github")


def f03(v):
    if v == "w":
        f = Fig("f03", v, 680, 268, F03_T, F03_D)
        f.status(664, 20, F03_ST)
        f.box("you", 16, 44, 180, 52, [("You", "lbl"), ("approve or fix", "sub")])
        f.box("agent", 16, 176, 180, 56, [("AI coding agent", "lbl"), ("Claude Code, Cursor", "sub")])
        f.box("check", 268, 176, 196, 56, [("gitpro agent-check", "lbl"), ("PreToolUse hook", "sub")], "box-accent")
        f.box("git", 512, 176, 152, 56, [("git commit", "lbl"), ("push, gh pr create", "sub")])
        f.route("u1", [(98, 176), (98, 96)])
        f.route("u2", [(114, 96), (114, 176)])
        f.label(88, 140, "ASKS", anchor="end")
        f.label(124, 140, "APPROVES")
        f.route("q1", hline(196, 268, 196))
        f.route("q2", hline(268, 196, 212))
        f.route("g1", hline(464, 512, 204))
        f.route("g-stop", hline(464, 486, 204), draw=False)
        f.xmark(484, 250, "DENIED")
        return f
    f = Fig("f03", v, 320, 442, F03_T, F03_D)
    f.status(316, 16, F03_ST)
    f.box("you", 40, 32, 240, 52, [("You", "lbl"), ("approve or fix", "sub")])
    f.box("agent", 40, 140, 240, 56, [("AI coding agent", "lbl"), ("Claude Code, Cursor", "sub")])
    f.box("check", 40, 252, 240, 56, [("gitpro agent-check", "lbl"), ("PreToolUse hook", "sub")], "box-accent")
    f.box("git", 40, 376, 240, 56, [("git commit", "lbl"), ("push, gh pr create", "sub")])
    f.route("u1", [(152, 140), (152, 84)])
    f.route("u2", [(168, 84), (168, 140)])
    f.label(144, 116, "ASKS", anchor="end")
    f.label(176, 116, "APPROVES")
    f.route("q1", [(152, 196), (152, 252)])
    f.route("q2", [(168, 252), (168, 196)])
    f.route("g1", [(160, 308), (160, 376)])
    f.route("g-stop", [(160, 308), (160, 336)], draw=False)
    f.xmark(182, 340, "DENIED")
    return f


# ---------------------------------------------------------------- FIG. 04: setup

F04_T = "Setup in four steps"
F04_D = ("gitpro setup asks a few questions and saves ~/.config/gitpro.toml. gitpro plan previews every change. "
         "gitpro apply writes a marked block in ~/.gitconfig, one file per identity and the SSH host entries. "
         "From then on git and ssh read those files themselves. gitpro uninstall removes them again.")
F04_ST = ("ANSWER A FEW QUESTIONS", "SAVED, NOTHING APPLIED YET", "PLAN SHOWS EVERY CHANGE",
          "APPLY WRITES PLAIN CONFIG", "GIT DOES THE MATCHING", "UNDO: gitpro uninstall")
STEPS = [("gitpro setup", "--write"), ("gitpro.toml", "~/.config"), ("gitpro plan", "preview only"),
         ("gitpro apply", "-y")]
OUTS = [("~/.gitconfig", "marked block"), ("per identity", "~/.gitconfig-<id>"), ("~/.ssh/config", "key per host")]


def f04(v):
    if v == "w":
        f = Fig("f04", v, 680, 234, F04_T, F04_D)
        f.status(664, 20, F04_ST)
        xs = [24, 188, 352, 516]
        for i, x in enumerate(xs):
            f.box(f"s{i + 1}", x, 40, 140, 56, [(STEPS[i][0], "lbl"), (STEPS[i][1], "sub")],
                  "box-accent" if i == 1 else "box")
            if i:
                f.route(f"k{i}", hline(xs[i - 1] + 140, x, 68))
        for i, x in enumerate([172, 344, 516]):
            f.box(f"o{i + 1}", x, 164, 148, 56, [(OUTS[i][0], "lbl"), (OUTS[i][1], "sub")])
        f.route("d3", [(590, 96), (590, 164)])
        f.route("d2", [(590, 96), (590, 130), (418, 130), (418, 164)])
        f.route("d1", [(590, 96), (590, 130), (246, 130), (246, 164)])
        f.box("w0", 24, 164, 124, 56, [("git, ssh", "lbl"), ("read these", "sub")])
        f.route("w", hline(172, 148, 192))
        return f
    f = Fig("f04", v, 320, 558, F04_T, F04_D)
    f.status(316, 16, F04_ST)
    for i in range(4):
        y = 30 + i * 70
        f.box(f"s{i + 1}", 40, y, 240, 48, [(STEPS[i][0], "lbl"), (STEPS[i][1], "sub")],
              "box-accent" if i == 1 else "box")
        if i:
            f.route(f"k{i}", [(160, y - 22), (160, y)])
    for i in range(3):
        y = 308 + i * 64
        f.box(f"o{i + 1}", 40, y, 240, 48, [(OUTS[i][0], "lbl"), (OUTS[i][1], "sub")])
        f.route(f"d{i + 1}", [(40, 264), (20, 264), (20, y + 24), (40, y + 24)])
    f.box("w0", 40, 502, 240, 48, [("git, ssh", "lbl"), ("read these", "sub")])
    f.route("w", [(280, 332), (300, 332), (300, 526), (280, 526)])
    return f


FIGS = {"f01": f01, "f02": f02, "f03": f03, "f04": f04}


def main() -> int:
    page = INDEX.read_text(encoding="utf-8")
    errs = []
    for name, build in FIGS.items():
        figs = [build("w"), build("n")]
        for f in figs:
            errs += f.check()
        block = "\n".join(f.svg() for f in figs)
        pat = re.compile(rf"(<!-- fig:{name} -->\n).*?(<!-- /fig:{name} -->)", re.S)
        if not pat.search(page):
            errs.append(f"index.html has no <!-- fig:{name} --> markers")
            continue
        page = pat.sub(lambda m: m[1] + block + "\n" + m[2], page)
    for e in errs:
        print(e, file=sys.stderr)
    if errs:
        return 1
    INDEX.write_text(page, encoding="utf-8")
    print(f"wrote {len(FIGS)} figures into {INDEX.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
