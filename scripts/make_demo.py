"""Build docs/demo.gif from real output: every frame is a real command run on the example files.

    uv run python scripts/make_demo.py        (needs Google Chrome or Chromium, and ffmpeg)

Scenes: map a messy export, preview day 1, load day 2, a truncated file stopped by the
guard, and the HTML report of the day-2 run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
ACME = ROOT / "examples" / "acme"
OUT = ROOT / "docs" / "demo.gif"
W, H = 1200, 720
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or "chromium"
THEME = TerminalTheme(
    (16, 22, 30), (230, 235, 241),
    [(16, 22, 30), (255, 123, 114), (126, 231, 135), (242, 204, 96), (121, 192, 255),
     (210, 168, 255), (118, 227, 234), (230, 235, 241)],
    [(110, 118, 129), (255, 161, 152), (86, 211, 100), (227, 179, 65), (165, 214, 255),
     (226, 197, 255), (179, 240, 255), (255, 255, 255)],
)  # fmt: skip


def sh(cmd: str, env: dict[str, str], cwd: Path) -> str:
    """Run a shell command, return its combined output with colors."""
    r = subprocess.run(
        cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    return r.stdout + r.stderr


def frame_html(title: str, command: str, output: str) -> str:
    console = Console(record=True, width=118, force_terminal=True, color_system="truecolor")
    console.print(Text(f"# {title}", style="bold #8b949e"))
    console.print(Text("$ ", style="bold #7ee787") + Text(command, style="bold"))
    console.print(Text.from_ansi(output.rstrip()))
    html = console.export_html(theme=THEME, inline_styles=True)
    return html.replace(
        "<body>",
        f"<body style='margin:0;width:{W}px;height:{H}px;overflow:hidden;"
        "background:#10161e;padding:22px 26px;box-sizing:border-box;"
        "font-size:14px;line-height:1.35'>",
    )


def screenshot(html_path: Path, png: Path, height: int = H) -> None:
    subprocess.run(
        [CHROME, "--headless=new", "--no-sandbox", "--hide-scrollbars",
         f"--window-size={W},{height}", f"--screenshot={png}", f"file://{html_path}"],
        check=True, capture_output=True,
    )  # fmt: skip


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="syncline-demo-"))
    data = work / "acme"
    shutil.copytree(ACME, data)
    head = (data / "employes_2026-11-02.csv").read_bytes().split(b"\n")[:120]
    (work / "employes_cut.csv").write_bytes(b"\n".join(head) + b"\n")
    env = {
        **os.environ,
        "APP_DB_URL": f"sqlite:///{work / 'app.db'}",
        "SYNCLINE_STATE": str(work / "state.db"),
        "COLUMNS": "118",
        "FORCE_COLOR": "1",
        "TERM": "xterm-256color",
    }
    bin_ = Path(sys.executable).parent / "syncline"
    flow = "acme_users.yaml"
    day1_run = f"syncline run {flow} -f employes_2026-11-01.csv"

    scenes = [
        ("A messy HR export: French headers, cp1252, ';'. Suggest a mapping, offline.",
         "syncline map employes_2026-11-01.csv --flow acme_users -o draft.yaml", 5.0),
        ("Preview day 1. Nothing is written; bad rows come back with a reason.",
         f"syncline run {flow} -f employes_2026-11-01.csv --dry-run", 5.5),
        ("Load it, then run the same file again.",
         f"{day1_run} && {day1_run}", 4.0),
        ("Day 2: only the changes go out. 6 hires, 2 moves, 4 leavers.",
         f"syncline run {flow} --report-dir reports", 5.5),
        ("A truncated export would switch off 199 people. The guard stops it.",
         f"syncline run {flow} -f ../employes_cut.csv", 5.5),
    ]  # fmt: skip
    pngs: list[tuple[Path, float]] = []
    for i, (title, cmd, secs) in enumerate(scenes):
        output = sh(cmd.replace("syncline ", f"{bin_} "), env, data)
        if "--dry-run" in cmd:
            output = "\n".join(output.splitlines()[:16]) + "\n  …"
        page = work / f"f{i}.html"
        page.write_text(frame_html(title, cmd, output), encoding="utf-8")
        png = work / f"f{i}.png"
        screenshot(page, png)
        pngs.append((png, secs))

    report = next((data / "reports").glob("*.html"))
    png = work / "report.png"
    screenshot(report, png)
    pngs.append((png, 5.0))

    listing = work / "frames.txt"
    listing.write_text(
        "".join(f"file '{p}'\nduration {d}\n" for p, d in pngs) + f"file '{pngs[-1][0]}'\n"
    )
    OUT.parent.mkdir(exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-vf", "fps=4,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];"
                "[b][p]paletteuse=dither=bayer:bayer_scale=4",
         str(OUT)],
        check=True,
    )  # fmt: skip
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB) from {work}")


if __name__ == "__main__":
    main()
