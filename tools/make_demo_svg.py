"""Render the README's terminal demo as an SVG, from live CLI output.

Why SVG rather than a GIF: it is a tenth the size, stays crisp at any width,
diffs as text in review, and - the part that matters here - it is regenerated
from the real command, so CI can rebuild it and fail if it has drifted from
what the tool actually prints. A screenshot nobody can re-derive is exactly the
sort of unverifiable claim this project avoids everywhere else.

The text is captured verbatim. Only the colours are applied here, mirroring the
severity styling the CLI uses on a real terminal.

    python tools/make_demo_svg.py docs/assets/demo.svg
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "examples" / "quickstart" / "base" / "target" / "manifest.json"
HEAD = ROOT / "examples" / "quickstart" / "head" / "target" / "manifest.json"

PROMPT = "$ "
COMMAND = "whatbreaks check --base base/target/manifest.json --head target/manifest.json"

# Terminal palette. Muted rather than neon: this sits in a README, not a demo reel.
BG = "#12141a"
CHROME = "#1b1e26"
FG = "#c9d1d9"
DIM = "#6e7681"
RED = "#f47067"
YELLOW = "#d8a657"
GREEN = "#7ee787"
CYAN = "#79c0ff"

CHAR_W = 8.4
LINE_H = 21.0
PAD_X = 22.0
PAD_TOP = 52.0
PAD_BOTTOM = 20.0


def capture() -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "whatbreaks.cli",
            "check",
            "--base",
            str(BASE),
            "--head",
            str(HEAD),
            "--format",
            "text",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if not result.stdout.strip():
        raise SystemExit(f"no output from the CLI:\n{result.stderr}")
    return result.stdout.rstrip("\n").split("\n")


def colour_for(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("BREAKING"):
        return RED
    if stripped.startswith("MAYBE"):
        return YELLOW
    if stripped.startswith("safe"):
        return GREEN
    if stripped.startswith("note:"):
        return YELLOW
    if stripped.startswith(("info", "analysed", "confidence:")) or stripped[:1].isdigit():
        return DIM
    return FG


def esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def build(lines: list[str]) -> str:
    rows = [PROMPT + COMMAND, *lines]
    width = PAD_X * 2 + CHAR_W * max(len(r) for r in rows)
    height = PAD_TOP + LINE_H * len(rows) + PAD_BOTTOM

    # Command types out, then output appears line by line. Each row's base
    # opacity is 1 and the keyframes start at 0, so if a renderer ignores CSS
    # animation the frame degrades to the finished state rather than to blank.
    type_seconds = 1.4
    per_line = 0.10
    total = type_seconds + per_line * len(lines) + 3.0

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        f'aria-label="whatbreaks detecting a breaking dbt change">',
        "<style>",
        "  .t { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;"
        " font-size: 13px; white-space: pre; }",
        "  .row { opacity: 1; }",
        f"  @keyframes reveal {{ from {{ opacity: 0 }} to {{ opacity: 1 }} }}",
        f"  @keyframes typing {{ from {{ width: 0 }} to {{ width: 100% }} }}",
        "  @media (prefers-reduced-motion: no-preference) {",
        f"    .row {{ animation: reveal 0.28s ease-out backwards; }}",
        "  }",
        "</style>",
        f'<rect width="{width:.0f}" height="{height:.0f}" rx="10" fill="{BG}"/>',
        f'<rect width="{width:.0f}" height="34" rx="10" fill="{CHROME}"/>',
        f'<rect y="24" width="{width:.0f}" height="10" fill="{CHROME}"/>',
        f'<circle cx="20" cy="17" r="5" fill="#ff5f57"/>',
        f'<circle cx="38" cy="17" r="5" fill="#febc2e"/>',
        f'<circle cx="56" cy="17" r="5" fill="#28c840"/>',
        # ASCII only in the title: this file is served as an image, and a
        # mojibake dash in the chrome is a needless way to look unfinished.
        f'<text class="t" x="{width / 2:.0f}" y="22" fill="{DIM}" '
        f'text-anchor="middle" font-size="11">whatbreaks / quickstart example</text>',
    ]

    y = PAD_TOP
    # the command line
    out.append(
        f'<g class="row" style="animation-duration:{type_seconds:.2f}s">'
        f'<text class="t" x="{PAD_X:.0f}" y="{y:.0f}">'
        f'<tspan fill="{GREEN}">{esc(PROMPT)}</tspan>'
        f'<tspan fill="{CYAN}">whatbreaks</tspan>'
        f'<tspan fill="{FG}">{esc(COMMAND[len("whatbreaks") :])}</tspan>'
        f"</text></g>"
    )

    for index, line in enumerate(lines):
        y += LINE_H
        if not line.strip():
            continue
        delay = type_seconds + per_line * index
        out.append(
            f'<g class="row" style="animation-delay:{delay:.2f}s">'
            f'<text class="t" x="{PAD_X:.0f}" y="{y:.0f}" fill="{colour_for(line)}">'
            f"{esc(line)}</text></g>"
        )

    # a resting cursor, so a static render still reads as a terminal
    out.append(
        f'<rect x="{PAD_X:.0f}" y="{y + 8:.0f}" width="8" height="15" fill="{DIM}">'
        f'<animate attributeName="opacity" values="1;0;1" dur="1.1s" '
        f'begin="{total - 3.0:.2f}s" repeatCount="indefinite"/></rect>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    destination = Path(args[0]) if args else ROOT / "docs/assets/demo.svg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    svg = build(capture())

    if "--check" in sys.argv:
        if not destination.exists() or destination.read_text(encoding="utf-8") != svg:
            print(f"STALE  {destination} does not match current CLI output")
            print("       regenerate with: python tools/make_demo_svg.py")
            return 1
        print(f"OK     {destination.name} matches current CLI output")
        return 0

    destination.write_text(svg, encoding="utf-8")
    print(f"wrote {destination.relative_to(ROOT)}  ({len(svg):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
