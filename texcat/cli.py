"""texcat — real LaTeX rendering, straight into your terminal.

Pipeline: LaTeX source -> latex (DVI) -> dvipng -> PNG -> terminal graphics
protocol (Kitty / iTerm2). Falls back to Unicode art (TeXicode / sympy) when
the terminal can't draw pixels. Output is genuine TeX typesetting — the same
engine and fonts Overleaf uses.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

__version__ = "0.7.0"
_RENDER_REV = 2  # bump when the render pipeline changes output for same input

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "texcat"
)

# environments that are already display-math environments (must not be
# wrapped in \[ \])
TOP_LEVEL_ENVS = re.compile(
    r"^\s*\\begin\{(align\*?|gather\*?|multline\*?|eqnarray\*?"
    r"|alignat\*?|flalign\*?|equation\*?)\}"
)

BASE_PACKAGES = ["amsmath", "amssymb", "mathtools"]

_PKG_OK = re.compile(r"^[A-Za-z0-9_-]+$")


def extra_packages(args_packages: str | None) -> list[str]:
    raw = args_packages or os.environ.get("TEXCAT_PACKAGES", "")
    return [p.strip() for p in raw.split(",")
            if p.strip() and _PKG_OK.match(p.strip())]


def build_doc(body: str, border: int, packages: list[str]) -> tuple[str, str]:
    """Return (full document, preamble) — the preamble keys the .fmt cache."""
    lines = [r"\documentclass[preview,border=%dpt]{standalone}" % border]
    for p in BASE_PACKAGES + [p for p in packages if p not in BASE_PACKAGES]:
        lines.append(r"\usepackage{%s}" % p)
    preamble = "\n".join(lines)
    doc = preamble + "\n\\begin{document}\n" + body + "\n\\end{document}\n"
    return doc, preamble


# --------------------------------------------------------------------------
# input handling
# --------------------------------------------------------------------------

def read_input(args: argparse.Namespace) -> str:
    if args.expr:
        return " ".join(args.expr)
    if sys.stdin.isatty():
        print("texcat: reading LaTeX from stdin — end with ctrl-d", file=sys.stderr)
    return sys.stdin.read()


def normalize(expr: str, inline: bool) -> str:
    """Strip user-supplied math delimiters and wrap appropriately."""
    e = expr.strip()
    for a, b in (("\\[", "\\]"), ("$$", "$$"), ("\\(", "\\)"), ("$", "$")):
        if e.startswith(a) and e.endswith(b) and len(e) > len(a) + len(b):
            e = e[len(a):-len(b)].strip()
            break
    if TOP_LEVEL_ENVS.match(e):
        return e
    if inline:
        return "$" + e + "$"
    return "\\[\n" + e + "\n\\]"


# --------------------------------------------------------------------------
# TeX -> PNG
# --------------------------------------------------------------------------

class TexError(RuntimeError):
    pass


def _tex_error_from_log(log_path: str) -> str:
    try:
        with open(log_path, errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return "latex failed (no log)"
    msgs = []
    for i, line in enumerate(lines):
        if line.startswith("! "):
            msgs.append(line.strip())
            # the l.<n> context line usually follows within a few lines
            for ctx in lines[i + 1 : i + 6]:
                if ctx.startswith("l."):
                    msgs.append("    " + ctx.strip())
                    break
    return "\n".join(msgs) or "latex failed (no error line found)"


def _autocrop(png: bytes, pad: int = 12) -> bytes:
    """Crop margins — display-math boxes span the full line width in TeX,
    so dvipng's tight mode still leaves wide horizontal flanks."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return png
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] < 255:  # transparent bg: crop on alpha
        bbox = alpha.getbbox()
    else:  # opaque bg: crop on difference from the corner color.
        # NB: diff in RGB, not RGBA — Pillow >= 10 getbbox() defaults to
        # alpha_only=True, and an opaque-vs-opaque diff has all-zero alpha,
        # which reads as "no bbox" even when plenty of ink differs.
        from PIL import ImageChops

        rgb = im.convert("RGB")
        bg_im = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
        bbox = ImageChops.difference(rgb, bg_im).getbbox()
    if bbox is None:
        return png
    left, top, right, bottom = bbox
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(im.width, right + pad)
    bottom = min(im.height, bottom + pad)
    out = io.BytesIO()
    im.crop((left, top, right, bottom)).save(out, "PNG")
    return out.getvalue()


def _ensure_format(latex: str, preamble: str) -> str | None:
    """Precompile the preamble into a .fmt once; later runs skip parsing it.
    Returns the cached .fmt path, or None if the build failed (plain runs
    still work)."""
    fmt_key = hashlib.sha256(preamble.encode()).hexdigest()[:16]
    fmt_path = os.path.join(CACHE_DIR, f"pre-{fmt_key}.fmt")
    fail_marker = fmt_path + ".failed"
    if os.path.exists(fmt_path):
        return fmt_path
    if os.path.exists(fail_marker):
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="texcat-fmt-") as tmp:
        with open(os.path.join(tmp, "job.tex"), "w") as f:
            f.write(preamble + "\n\\begin{document}\nx\n\\end{document}\n")
        proc = subprocess.run(
            [latex, "-ini", "-interaction=nonstopmode", "-jobname=pre",
             "&latex", "mylatexformat.ltx", "job.tex"],
            cwd=tmp, capture_output=True, timeout=60,
        )
        built = os.path.join(tmp, "pre.fmt")
        if proc.returncode == 0 and os.path.exists(built):
            shutil.move(built, fmt_path)
            return fmt_path
    open(fail_marker, "w").close()
    return None


def render_png(
    body: str,
    dpi: int,
    fg: str,
    bg: str,
    border: int = 4,
    packages: list[str] | None = None,
) -> bytes:
    doc, preamble = build_doc(body, border, packages or [])
    key = hashlib.sha256(
        f"{doc}|{dpi}|{fg}|{bg}|r{_RENDER_REV}".encode()
    ).hexdigest()[:24]
    cached = os.path.join(CACHE_DIR, key + ".png")
    if os.path.exists(cached):
        with open(cached, "rb") as f:
            return f.read()

    latex = shutil.which("latex")
    dvipng = shutil.which("dvipng")
    if not latex or not dvipng:
        raise TexError(
            "latex/dvipng not found — install TeX Live (or MacTeX) for "
            "pixel rendering, or use --unicode"
        )

    fmt_path = _ensure_format(latex, preamble)

    with tempfile.TemporaryDirectory(prefix="texcat-") as tmp:
        tex_path = os.path.join(tmp, "job.tex")
        with open(tex_path, "w") as f:
            f.write(doc)

        def run_latex(with_fmt: bool):
            cmd = [latex]
            if with_fmt and fmt_path:
                os.symlink(fmt_path, os.path.join(tmp, "texcatpre.fmt"))
                cmd += ["-fmt", "texcatpre"]
            cmd += ["-interaction=nonstopmode", "-halt-on-error", "job.tex"]
            return subprocess.run(cmd, cwd=tmp, capture_output=True, timeout=30)

        proc = run_latex(with_fmt=True)
        if proc.returncode != 0 and fmt_path:
            # stale/broken format: drop it and retry plain
            try:
                os.unlink(fmt_path)
            except OSError:
                pass
            fmt_path = None
            proc = run_latex(with_fmt=False)
        if proc.returncode != 0:
            raise TexError(_tex_error_from_log(os.path.join(tmp, "job.log")))
        proc = subprocess.run(
            [
                dvipng, "-q", "-T", "tight", "-D", str(dpi),
                "--truecolor", "-z", "6",
                "-bg", bg, "-fg", fg,
                "-o", "job.png", "job.dvi",
            ],
            cwd=tmp,
            capture_output=True,
            timeout=30,
        )
        png_path = os.path.join(tmp, "job.png")
        if proc.returncode != 0 or not os.path.exists(png_path):
            raise TexError(
                "dvipng failed: " + proc.stderr.decode(errors="replace")[:400]
            )
        with open(png_path, "rb") as f:
            png = f.read()

    png = _autocrop(png)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cached, "wb") as f:
        f.write(png)
    return png


# --------------------------------------------------------------------------
# terminal theme detection
# --------------------------------------------------------------------------

def terminal_bg_luminance(timeout: float = 0.15) -> float | None:
    """Query the terminal background color via OSC 11. None if unknown."""
    if not sys.stdout.isatty():
        return None
    try:
        import select
        import termios
        import tty

        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None
    try:
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            os.write(fd, b"\x1b]11;?\x1b\\")
            buf = b""
            while select.select([fd], [], [], timeout)[0]:
                buf += os.read(fd, 256)
                if b"\x07" in buf or b"\x1b\\" in buf:
                    break
            m = re.search(
                rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", buf
            )
            if not m:
                return None
            r, g, b = (int(x[:2], 16) / 255.0 for x in m.groups())
            return 0.2126 * r + 0.7152 * g + 0.0722 * b
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return None
    finally:
        os.close(fd)


def resolve_theme(theme: str) -> tuple[str, str]:
    """Return (fg, bg) in dvipng color syntax for the requested theme."""
    if theme == "card":
        return "rgb 0.0 0.0 0.0", "rgb 1.0 1.0 1.0"
    if theme == "light":
        return "rgb 0.0 0.0 0.0", "Transparent"
    if theme == "dark":
        return "rgb 0.92 0.92 0.92", "Transparent"
    # auto: blend with the real terminal background when detectable
    lum = terminal_bg_luminance()
    if lum is None:
        return "rgb 0.0 0.0 0.0", "rgb 1.0 1.0 1.0"  # card is always legible
    if lum < 0.5:
        return "rgb 0.92 0.92 0.92", "Transparent"
    return "rgb 0.0 0.0 0.0", "Transparent"


# --------------------------------------------------------------------------
# display backends
# --------------------------------------------------------------------------

def graphics_protocol() -> str | None:
    """Detect which inline-image protocol the terminal speaks."""
    term = os.environ.get("TERM", "")
    prog = os.environ.get("TERM_PROGRAM", "")
    if "kitty" in term or "ghostty" in term or os.environ.get("KITTY_WINDOW_ID"):
        return "kitty"
    if prog in ("ghostty", "WezTerm", "kitty"):
        return "kitty"
    if prog == "iTerm.app" or os.environ.get("ITERM_SESSION_ID"):
        return "iterm"
    return None


def _tty_out():
    if sys.stdout.isatty():
        return sys.stdout.buffer
    try:
        return open("/dev/tty", "wb")
    except OSError:
        return None


def png_size(png: bytes) -> tuple[int, int]:
    """Width/height straight from the IHDR chunk — no decoder needed."""
    import struct

    if len(png) > 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", png[16:24])
        return w, h
    return 0, 0


def tty_cell_geometry() -> tuple[int, int, float] | None:
    """(rows, cols, px_per_col) of the output terminal, if knowable."""
    import fcntl
    import struct
    import termios

    for fd in (1, 0, 2):
        try:
            ws = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols, xp, yp = struct.unpack("HHHH", ws)
            if cols > 0 and xp > 0:
                return rows, cols, xp / cols
            if cols > 0:
                return rows, cols, 0.0
        except OSError:
            continue
    return None


def fit_columns(png: bytes, requested: int | None) -> int | None:
    """Pick a c= column count so the image never overflows the window."""
    if requested:
        return requested
    geo = tty_cell_geometry()
    if not geo:
        return None
    _, cols, px_per_col = geo
    if px_per_col <= 0:
        return None
    img_w, _ = png_size(png)
    if not img_w:
        return None
    img_cols = img_w / px_per_col
    max_cols = max(cols - 2, 10)
    if img_cols > max_cols:
        return max_cols
    return None  # fits at natural size — keep it pixel-crisp


def emit_kitty(png: bytes, cols: int | None = None) -> bool:
    out = _tty_out()
    if out is None:
        return False
    data = base64.standard_b64encode(png)
    extra = f",c={cols}" if cols else ""
    first = True
    i = 0
    while i < len(data):
        chunk = data[i : i + 4096]
        i += 4096
        more = 1 if i < len(data) else 0
        ctrl = f"a=T,f=100,m={more}{extra}" if first else f"m={more}"
        out.write(b"\x1b_G" + ctrl.encode() + b";" + chunk + b"\x1b\\")
        first = False
    out.write(b"\n")
    out.flush()
    return True


def emit_iterm(png: bytes, cols: int | None = None) -> bool:
    out = _tty_out()
    if out is None:
        return False
    b64 = base64.standard_b64encode(png)
    width = b";width=%d" % cols if cols else b""
    out.write(
        b"\x1b]1337;File=inline=1;size=%d%s:%s\x07\n" % (len(png), width, b64)
    )
    out.flush()
    return True


# --------------------------------------------------------------------------
# unicode fallback tier
# --------------------------------------------------------------------------

MATRIX_RE = re.compile(
    r"\\begin\{([pbvBV]?matrix)\}(.*?)\\end\{\1\}", re.DOTALL
)


def _preprocess_unicode(expr: str) -> str:
    # TeXicode renders \pmod literally; expand it ourselves
    expr = re.sub(r"\\pmod\s*\{([^}]*)\}", r"\\ (\\mathrm{mod}\\ \1)", expr)
    expr = re.sub(r"\\pmod\s+(\w+)", r"\\ (\\mathrm{mod}\\ \1)", expr)
    return expr


def _sympy_cell(cell: str):
    import sympy

    cell = cell.strip()
    try:
        from sympy.parsing.latex import parse_latex

        return parse_latex(cell)
    except Exception:
        pass
    try:
        return sympy.sympify(cell)
    except Exception:
        return sympy.Symbol(cell or "?")


def _unicode_matrix(expr: str) -> str | None:
    """Render `lhs = <matrix>` or a bare matrix via sympy pretty-printing."""
    m = MATRIX_RE.search(expr)
    if not m:
        return None
    try:
        import sympy
    except ImportError:
        return None
    rows = [
        [_sympy_cell(c) for c in row.split("&")]
        for row in m.group(2).strip().split("\\\\")
        if row.strip()
    ]
    try:
        mat = sympy.Matrix(rows)
    except Exception:
        return None
    before = expr[: m.start()].strip().rstrip("=").strip()
    after = expr[m.end() :].strip()
    if after:  # matrix embedded mid-expression: too hard for this tier
        return None
    rendered = sympy.pretty(mat, use_unicode=True)
    if before:
        pad = rendered.splitlines()
        mid = len(pad) // 2
        prefix = before + " = "
        out = []
        for i, line in enumerate(pad):
            out.append((prefix if i == mid else " " * len(prefix)) + line)
        return "\n".join(out)
    return rendered


def render_unicode(expr: str) -> str:
    expr = _preprocess_unicode(expr)

    mat = _unicode_matrix(expr)
    if mat is not None:
        return mat

    txc = shutil.which("txc")
    if txc:
        proc = subprocess.run(
            [txc, expr], capture_output=True, text=True, timeout=15
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.rstrip("\n")

    try:
        import sympy
        from sympy.parsing.latex import parse_latex

        return sympy.pretty(parse_latex(expr), use_unicode=True)
    except Exception:
        pass

    return expr  # last resort: give back what we got


# --------------------------------------------------------------------------
# HUD mode: float an equation over a TUI (e.g. Claude Code) on its own pty
# --------------------------------------------------------------------------

HUD_IMAGE_ID = 133742  # fixed id: every HUD send replaces the previous one


def find_session_tty() -> str | None:
    """Walk up the process tree to the pty this session is displayed on.
    Works from TTY-less subprocesses (agent harnesses, launchd jobs)."""
    pid = os.getpid()
    for _ in range(20):
        try:
            out = subprocess.run(
                ["ps", "-o", "tty=", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.split()
        except Exception:
            return None
        if len(out) < 2:
            return None
        tty, ppid = out[0], out[1]
        if tty not in ("??", "-"):
            return "/dev/" + tty
        try:
            pid = int(ppid)
        except ValueError:
            return None
        if pid <= 1:
            return None
    return None


def emit_hud(png: bytes, tty_path: str, z: int = 1000) -> bool:
    """Overlay the image at the top-right of the target terminal.

    The placement draws ABOVE text (z>0) and survives the TUI's repaints —
    only scrolling or an explicit clear removes it. Cursor is saved/restored
    and C=1 keeps the placement itself from moving it, so the host TUI's
    cursor bookkeeping is undisturbed.
    """
    import fcntl
    import struct
    import termios

    try:
        fd = os.open(tty_path, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return False
    try:
        col = 1
        try:
            ws = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
            _, cols, xp, _ = struct.unpack("HHHH", ws)
            img_w, _h = png_size(png)
            if cols and xp and img_w:
                img_cols = int(img_w / (xp / cols)) + 1
                col = max(1, cols - img_cols)
        except OSError:
            pass
        data = base64.standard_b64encode(png)
        buf = bytearray()
        buf += b"\x1b7"                      # save cursor
        buf += b"\x1b[1;%dH" % col           # park at row 1, right-aligned
        first = True
        i = 0
        while i < len(data):
            chunk = data[i : i + 4096]
            i += 4096
            more = 1 if i < len(data) else 0
            if first:
                ctrl = (f"a=T,f=100,q=2,C=1,z={z},"
                        f"i={HUD_IMAGE_ID},p=1,m={more}")
            else:
                ctrl = f"m={more}"
            buf += b"\x1b_G" + ctrl.encode() + b";" + chunk + b"\x1b\\"
            first = False
        buf += b"\x1b8"                      # restore cursor
        os.write(fd, bytes(buf))
        return True
    finally:
        os.close(fd)


def emit_hud_clear(tty_path: str) -> bool:
    try:
        fd = os.open(tty_path, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return False
    try:
        os.write(fd, b"\x1b_Ga=d,d=i,i=%d,q=2\x1b\\" % HUD_IMAGE_ID)
        return True
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# markdown file mode: -f / --watch
# --------------------------------------------------------------------------

MD_MATH_RE = re.compile(
    r"(\$\$.*?\$\$|\\\[.*?\\\]|```math\n.*?```)", re.DOTALL
)


def _strip_math_block(block: str) -> str:
    if block.startswith("```math"):
        return block[len("```math"):].strip("`\n ")
    return block  # $$..$$ / \[..\] are stripped by normalize()


def render_file_once(path: str, args, fg: str, bg: str) -> None:
    try:
        text = open(path).read()
    except OSError as e:
        print(f"texcat: cannot read {path}: {e}", file=sys.stderr)
        return
    proto = graphics_protocol()
    for part in MD_MATH_RE.split(text):
        if MD_MATH_RE.fullmatch(part):
            expr = _strip_math_block(part)
            body = normalize(expr, inline=False)
            try:
                png = render_png(body, dpi=args.dpi, fg=fg, bg=bg,
                                 packages=extra_packages(args.packages))
                cols = fit_columns(png, args.cols)
                shown = (
                    emit_kitty(png, cols) if proto == "kitty"
                    else emit_iterm(png, cols) if proto == "iterm"
                    else False
                )
                if not shown:
                    print(render_unicode(expr))
            except TexError as e:
                print(f"⚠ TeX rejected block:\n{e}")
                print(render_unicode(expr))
        else:
            sys.stdout.write(part)
            sys.stdout.flush()


def render_file(args) -> int:
    fg, bg = resolve_theme(args.theme)
    if not args.watch:
        render_file_once(args.file, args, fg, bg)
        return 0
    import time

    print(f"texcat: watching {args.file} — ctrl-c to quit")
    last = 0.0
    try:
        while True:
            try:
                mtime = os.path.getmtime(args.file)
            except OSError:
                time.sleep(0.3)
                continue
            if mtime != last:
                last = mtime
                out = _tty_out()
                if out is not None:
                    # delete kitty images, then clear + home
                    out.write(b"\x1b_Ga=d\x1b\\\x1b[2J\x1b[H")
                    out.flush()
                render_file_once(args.file, args, fg, bg)
            time.sleep(0.3)
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------
# live viewer: --listen / --send
# --------------------------------------------------------------------------

FEED_PATH = os.path.join(CACHE_DIR, "feed.jsonl")


def send_to_viewer(
    expr: str, theme: str, dpi: int,
    cols: int | None = None, packages: str | None = None,
) -> None:
    import json

    os.makedirs(CACHE_DIR, exist_ok=True)
    item = {"tex": expr, "theme": theme, "dpi": dpi}
    if cols:
        item["cols"] = cols
    if packages:
        item["packages"] = packages
    with open(FEED_PATH, "a") as f:
        f.write(json.dumps(item) + "\n")


def listen_loop(args) -> int:
    """Run as a live math viewport: render every expression appended to the
    feed file. Meant to run in its own terminal window/split, e.g.:

        open -na Ghostty --args -e texcat --listen
    """
    import json
    import time

    os.makedirs(CACHE_DIR, exist_ok=True)
    open(FEED_PATH, "a").close()  # ensure it exists
    proto = graphics_protocol()
    log_path = os.path.join(CACHE_DIR, "viewer.log")

    def vlog(msg: str) -> None:
        with open(log_path, "a") as lf:
            lf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

    print("texcat viewer — waiting for math… (ctrl-c to quit)")
    print(f"push with: texcat --send '<latex>'   [feed: {FEED_PATH}]\n")
    vlog(f"viewer started pid={os.getpid()} proto={proto or 'none'}")
    theme_cache: dict[str, tuple[str, str]] = {}  # OSC query once, not per item
    pos = os.path.getsize(FEED_PATH)  # only render NEW entries
    try:
        while True:
            size = os.path.getsize(FEED_PATH)
            if size < pos:  # feed was truncated/rotated
                pos = 0
            if size > pos:
                with open(FEED_PATH) as f:
                    f.seek(pos)
                    lines = f.read()
                    pos = f.tell()
                for line in lines.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        item = {"tex": line}
                    cmd = item.get("cmd")
                    if cmd == "clear":
                        out = _tty_out()
                        if out is not None:
                            out.write(b"\x1b_Ga=d\x1b\\\x1b[2J\x1b[H")
                            out.flush()
                        print("texcat viewer — cleared\n")
                        vlog("cleared")
                        continue
                    if cmd == "note":
                        note = str(item.get("text", ""))
                        print(f"\x1b[2m── {note} ──\x1b[0m")
                        vlog(f"note: {note[:70]}")
                        continue
                    expr = item.get("tex", "")
                    if not expr.strip():
                        continue
                    body = normalize(expr, inline=False)
                    try:
                        theme = item.get("theme", args.theme)
                        if theme not in theme_cache:
                            theme_cache[theme] = resolve_theme(theme)
                        fg, bg = theme_cache[theme]
                        png = render_png(
                            body, dpi=int(item.get("dpi", args.dpi)),
                            fg=fg, bg=bg,
                            packages=extra_packages(
                                item.get("packages") or args.packages
                            ),
                        )
                        cols = fit_columns(png, item.get("cols") or args.cols)
                        shown = (
                            emit_kitty(png, cols) if proto == "kitty"
                            else emit_iterm(png, cols) if proto == "iterm"
                            else False
                        )
                        if not shown:
                            print(render_unicode(expr))
                        elif not args.plain:
                            src = " ".join(expr.split())
                            geo = tty_cell_geometry()
                            width = (geo[1] if geo else 80) - 4
                            if len(src) > width:
                                src = src[: width - 1] + "…"
                            print(f"\x1b[2m{src}\x1b[0m")
                        vlog(f"rendered ({'pixels' if shown else 'unicode'}, "
                             f"{len(png)}B png): {expr[:70]}")
                    except TexError as e:
                        print(f"⚠ TeX rejected: {expr[:60]}\n{e}\n")
                        print(render_unicode(expr))
                        vlog(f"TeX-rejected: {expr[:70]}")
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\ntexcat viewer: bye")
        return 0


def spawn_viewer_window() -> bool:
    """Best-effort: open a new terminal window running the viewer (macOS)."""
    if sys.platform != "darwin":
        return False
    term = os.environ.get("TERM_PROGRAM", "")
    if "ghostty" in term.lower() or "ghostty" in os.environ.get("TERM", ""):
        app = "Ghostty"
    elif term == "iTerm.app":
        app = "iTerm"
    else:
        app = "Ghostty"
    exe = shutil.which("texcat") or "texcat"
    r = subprocess.run(
        ["open", "-na", app, "--args", "-e", exe, "--listen"],
        capture_output=True,
    )
    return r.returncode == 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="texcat",
        description="Render LaTeX math in the terminal at Overleaf fidelity "
        "(real TeX -> pixels via Kitty/iTerm2 graphics), with a Unicode-art "
        "fallback for plain terminals.",
    )
    p.add_argument("expr", nargs="*", help="LaTeX expression (or pipe via stdin)")
    p.add_argument("-u", "--unicode", action="store_true",
                   help="force the Unicode-art tier (no graphics)")
    p.add_argument("-i", "--inline", action="store_true",
                   help="typeset in inline (text) style instead of display style")
    p.add_argument("-d", "--dpi", type=int, default=280,
                   help="render resolution (default 280; bump for bigger output)")
    p.add_argument("-c", "--cols", type=int, default=None,
                   help="display width in terminal columns (default: natural "
                   "size, auto-shrunk to fit the window)")
    p.add_argument("-p", "--packages", metavar="P1,P2",
                   help="extra LaTeX packages to load (also via "
                   "TEXCAT_PACKAGES env), e.g. physics,siunitx")
    p.add_argument("-t", "--theme", choices=["auto", "card", "dark", "light"],
                   default="auto",
                   help="auto = match terminal bg (transparent); card = "
                   "black-on-white like a paper snippet")
    p.add_argument("-o", "--out", metavar="PNG",
                   help="write the PNG to a file instead of displaying")
    p.add_argument("--open", action="store_true",
                   help="open the render in the system image viewer")
    p.add_argument("--source", action="store_true",
                   help="also echo the LaTeX source beneath the render")
    p.add_argument("-f", "--file", metavar="MD",
                   help="render a markdown/text file: display-math blocks "
                   "($$…$$, \\[…\\], ```math fences) become typeset images, "
                   "prose passes through")
    p.add_argument("--watch", action="store_true",
                   help="with -f: re-render on every save (live preview)")
    p.add_argument("--listen", action="store_true",
                   help="run as a live viewer: render everything pushed to "
                   "the feed (use in a spare terminal window/split)")
    p.add_argument("--send", action="store_true",
                   help="push the expression to a running --listen viewer "
                   "instead of rendering here")
    p.add_argument("--viewer", action="store_true",
                   help="open a new terminal window running --listen (macOS)")
    p.add_argument("--hud", action="store_true",
                   help="float the equation over THIS session's terminal "
                   "(top-right, above the TUI) — works even from TTY-less "
                   "subprocesses like agent harnesses")
    p.add_argument("--hud-clear", action="store_true",
                   help="remove the HUD overlay")
    p.add_argument("--tty", metavar="/dev/ttysNNN",
                   help="target pty for --hud (default: auto-detect via "
                   "process ancestry)")
    p.add_argument("--send-clear", action="store_true",
                   help="tell the viewer to clear its screen")
    p.add_argument("--send-note", metavar="TEXT",
                   help="print a dim section note in the viewer")
    p.add_argument("--plain", action="store_true",
                   help="viewer: don't echo the LaTeX source under renders")
    p.add_argument("-V", "--version", action="version",
                   version=f"texcat {__version__}")
    args = p.parse_args(argv)

    if args.listen:
        return listen_loop(args)
    if args.file:
        return render_file(args)
    if args.viewer:
        ok = spawn_viewer_window()
        print("texcat: viewer window opened" if ok
              else "texcat: could not open viewer window", file=sys.stderr)
        if not args.expr:
            return 0 if ok else 1
        import time

        time.sleep(2.0)  # give the listener a beat before the first send
        send_to_viewer(" ".join(args.expr), args.theme, args.dpi,
                       args.cols, args.packages)
        return 0
    if args.hud or args.hud_clear:
        tty_path = args.tty or find_session_tty()
        if not tty_path:
            print("texcat: --hud: could not locate a session tty "
                  "(pass --tty /dev/ttysNNN)", file=sys.stderr)
            return 1
        if args.hud_clear and not (args.hud or args.expr):
            ok = emit_hud_clear(tty_path)
            print(f"texcat: HUD cleared on {tty_path}" if ok
                  else f"texcat: cannot write {tty_path}", file=sys.stderr)
            return 0 if ok else 1
        raw = read_input(args)
        if not raw.strip():
            p.error("no LaTeX given")
        theme = "card" if args.theme == "auto" else args.theme
        fg, bg = resolve_theme(theme)
        try:
            png = render_png(normalize(raw, inline=args.inline),
                             dpi=args.dpi, fg=fg, bg=bg,
                             packages=extra_packages(args.packages))
        except TexError as e:
            print(f"texcat: TeX rejected the input:\n{e}", file=sys.stderr)
            return 1
        ok = emit_hud(png, tty_path)
        print(f"texcat: HUD placed on {tty_path}" if ok
              else f"texcat: cannot write {tty_path}", file=sys.stderr)
        return 0 if ok else 1
    if args.send_clear or args.send_note:
        import json

        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(FEED_PATH, "a") as f:
            if args.send_clear:
                f.write(json.dumps({"cmd": "clear"}) + "\n")
            if args.send_note:
                f.write(json.dumps({"cmd": "note", "text": args.send_note})
                        + "\n")
        if not args.expr and not args.send:
            return 0
    if args.send:
        raw = read_input(args)
        if not raw.strip():
            p.error("no LaTeX given")
        send_to_viewer(raw, args.theme, args.dpi, args.cols, args.packages)
        return 0

    raw = read_input(args)
    if not raw.strip():
        p.error("no LaTeX given")

    body = normalize(raw, inline=args.inline)

    wants_pixels = not args.unicode and (
        args.out or args.open or graphics_protocol()
    )

    if wants_pixels:
        fg, bg = resolve_theme(args.theme)
        try:
            png = render_png(body, dpi=args.dpi, fg=fg, bg=bg,
                             packages=extra_packages(args.packages))
        except TexError as e:
            print(f"texcat: TeX rejected the input:\n{e}", file=sys.stderr)
            print("texcat: falling back to Unicode tier\n", file=sys.stderr)
            print(render_unicode(raw))
            return 1
        if args.out:
            with open(args.out, "wb") as f:
                f.write(png)
            print(f"texcat: wrote {args.out}", file=sys.stderr)
        if args.open:
            path = args.out
            if not path:
                os.makedirs(CACHE_DIR, exist_ok=True)
                path = os.path.join(CACHE_DIR, "last.png")
                with open(path, "wb") as f:
                    f.write(png)
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.run([opener, path], check=False)
        if not args.out and not args.open:
            proto = graphics_protocol()
            cols = fit_columns(png, args.cols)
            shown = emit_kitty(png, cols) if proto == "kitty" else (
                emit_iterm(png, cols) if proto == "iterm" else False
            )
            if not shown:
                print(render_unicode(raw))
        if args.source:
            print(raw.strip())
        return 0

    print(render_unicode(raw))
    if args.source:
        print("\n" + raw.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
