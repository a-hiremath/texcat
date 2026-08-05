"""Tests for texcat's pure-logic core.

Pixel rendering (render_png -> latex/dvipng) is skipped when a TeX
distribution is unavailable, so the full suite runs anywhere.
"""

import io
import json
import os
import sys
import shutil

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import texcat.cli as cli

HAS_TEX = bool(shutil.which("latex") and shutil.which("dvipng"))


def make_args(**kw):
    class NS:
        pass
    ns = NS()
    defaults = dict(
        expr=[], unicode=False, inline=False, dpi=280, border=4, cols=None,
        theme="auto", out=None, open=False, source=False, file=None,
        watch=False, listen=False, send=False, viewer=False,
    )
    defaults.update(kw)
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_plain_display(self):
        assert cli.normalize("x^2", inline=False) == "\\[\nx^2\n\\]"

    def test_plain_inline(self):
        assert cli.normalize("x^2", inline=True) == "$x^2$"

    def test_strips_square_brackets(self):
        assert cli.normalize("\\[ x=1 \\]", inline=False) == "\\[\nx=1\n\\]"

    def test_strips_dollar_dollars(self):
        assert cli.normalize("$$ x=2 $$", inline=False) == "\\[\nx=2\n\\]"

    def test_strips_single_dollar(self):
        assert cli.normalize("$ x=3 $", inline=True) == "$x=3$"

    def test_strips_parens(self):
        assert cli.normalize("\\( x=4 \\)", inline=False) == "\\[\nx=4\n\\]"

    def test_toplevel_env_not_wrapped(self):
        src = "\\begin{align} a &= b \\end{align}"
        assert cli.normalize(src, inline=True) == src

    def test_toplevel_env_starred(self):
        src = "\\begin{equation*} e = mc^2 \\end{equation*}"
        assert cli.normalize(src, inline=False) == src

    def test_whitespace_padding(self):
        assert cli.normalize("  x  ", inline=True) == "$x$"


# ---------------------------------------------------------------------------
# read_input
# ---------------------------------------------------------------------------

class TestReadInput:
    def test_from_expr_args(self):
        assert cli.read_input(make_args(expr=["a", "+", "b"])) == "a + b"

    def test_from_stdin(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("e^{i\\pi}"))
        assert cli.read_input(make_args(expr=[])) == "e^{i\\pi}"


# ---------------------------------------------------------------------------
# _tex_error_from_log
# ---------------------------------------------------------------------------

class TestTexErrorFromLog:
    def test_extracts_error_lines(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text(
            "! Undefined control sequence.\n"
            "l.6 \\badcmd\n"
            "The control sequence at the end of the top line\n"
            "of your error message was never \\def'ed.\n"
        )
        msg = cli._tex_error_from_log(str(log))
        assert "Undefined control sequence" in msg
        assert "l.6" in msg

    def test_missing_log(self, tmp_path):
        msg = cli._tex_error_from_log(str(tmp_path / "nope.log"))
        assert "no log" in msg

    def test_no_error_lines(self, tmp_path):
        log = tmp_path / "job.log"
        log.write_text("Output written on job.dvi (1 page).\n")
        assert "no error line" in cli._tex_error_from_log(str(log))


# ---------------------------------------------------------------------------
# theme resolution
# ---------------------------------------------------------------------------

class TestResolveTheme:
    def test_card(self):
        assert cli.resolve_theme("card") == ("rgb 0.0 0.0 0.0", "rgb 1.0 1.0 1.0")

    def test_light(self):
        assert cli.resolve_theme("light") == ("rgb 0.0 0.0 0.0", "Transparent")

    def test_dark(self):
        assert cli.resolve_theme("dark") == ("rgb 0.92 0.92 0.92", "Transparent")

    def test_auto_unknown_terminal_defaults_card(self, monkeypatch):
        monkeypatch.setattr(cli, "terminal_bg_luminance", lambda: None)
        assert cli.resolve_theme("auto") == ("rgb 0.0 0.0 0.0", "rgb 1.0 1.0 1.0")

    def test_auto_dark_terminal(self, monkeypatch):
        monkeypatch.setattr(cli, "terminal_bg_luminance", lambda: 0.1)
        assert cli.resolve_theme("auto") == ("rgb 0.92 0.92 0.92", "Transparent")

    def test_auto_light_terminal(self, monkeypatch):
        monkeypatch.setattr(cli, "terminal_bg_luminance", lambda: 0.9)
        assert cli.resolve_theme("auto") == ("rgb 0.0 0.0 0.0", "Transparent")


# ---------------------------------------------------------------------------
# graphics protocol detection
# ---------------------------------------------------------------------------

class TestGraphicsProtocol:
    def test_kitty_env(self, monkeypatch):
        monkeypatch.setenv("TERM", "xterm-kitty")
        assert cli.graphics_protocol() == "kitty"

    def test_ghostty_env(self, monkeypatch):
        monkeypatch.setenv("TERM", "ghostty")
        assert cli.graphics_protocol() == "kitty"

    def test_iterm_env(self, monkeypatch):
        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
        assert cli.graphics_protocol() == "iterm"

    def test_none(self, monkeypatch):
        monkeypatch.delenv("TERM", raising=False)
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
        monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
        monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
        assert cli.graphics_protocol() is None


# ---------------------------------------------------------------------------
# png_size / fit_columns
# ---------------------------------------------------------------------------

class TestPngSize:
    def _png(self, w, h):
        return (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00"
        )

    def test_parses_ihdr(self):
        assert cli.png_size(self._png(320, 200)) == (320, 200)

    def test_garbage(self):
        assert cli.png_size(b"not a png") == (0, 0)

    def test_short_buffer(self):
        assert cli.png_size(b"\x89PNG") == (0, 0)


class TestFitColumns:
    def _png(self, w, h):
        return (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00"
        )

    def test_requested_wins(self, monkeypatch):
        monkeypatch.setattr(cli, "tty_cell_geometry", lambda: (24, 80, 8.0))
        assert cli.fit_columns(self._png(1000, 100), 40) == 40

    def test_no_geometry(self, monkeypatch):
        monkeypatch.setattr(cli, "tty_cell_geometry", lambda: None)
        assert cli.fit_columns(self._png(1000, 100), None) is None

    def test_small_image_untouched(self, monkeypatch):
        monkeypatch.setattr(cli, "tty_cell_geometry", lambda: (24, 80, 8.0))
        assert cli.fit_columns(self._png(100, 100), None) is None

    def test_wide_image_capped(self, monkeypatch):
        monkeypatch.setattr(cli, "tty_cell_geometry", lambda: (24, 80, 8.0))
        assert cli.fit_columns(self._png(2000, 100), None) == 78


# ---------------------------------------------------------------------------
# unicode fallback tier
# ---------------------------------------------------------------------------

class TestUnicodeTier:
    def test_preprocess_pmod_braced(self):
        assert cli._preprocess_unicode("a \\pmod{n}") == "a \\ (\\mathrm{mod}\\ n)"

    def test_preprocess_pmod_bare(self):
        assert cli._preprocess_unicode("a \\pmod n") == "a \\ (\\mathrm{mod}\\ n)"

    def test_matrix_regex(self):
        m = cli.MATRIX_RE.search("A = \\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}")
        assert m is not None
        assert m.group(1) == "pmatrix"

    def test_render_unicode_fallback_returns_input(self):
        out = cli.render_unicode("\\not-a-real-command{")
        assert "\\not-a-real-command" in out


# ---------------------------------------------------------------------------
# emit / tty helpers
# ---------------------------------------------------------------------------

class TestEmit:
    def test_emit_kitty_chunks(self, monkeypatch):
        buf = io.BytesIO()
        monkeypatch.setattr(cli, "_tty_out", lambda: buf)
        png = b"\x89PNG" + os.urandom(20000)
        assert cli.emit_kitty(png) is True
        data = buf.getvalue()
        assert data.startswith(b"\x1b_G")
        assert data.count(b"\x1b_G") > 1

    def test_emit_kitty_cols(self, monkeypatch):
        buf = io.BytesIO()
        monkeypatch.setattr(cli, "_tty_out", lambda: buf)
        assert cli.emit_kitty(b"\x89PNG" + os.urandom(10), cols=40) is True
        assert b"c=40" in buf.getvalue()

    def test_emit_iterm(self, monkeypatch):
        buf = io.BytesIO()
        monkeypatch.setattr(cli, "_tty_out", lambda: buf)
        png = b"\x89PNG" + os.urandom(100)
        assert cli.emit_iterm(png) is True
        assert buf.getvalue().startswith(b"\x1b]1337;File=inline=1")

    def test_emit_iterm_cols(self, monkeypatch):
        buf = io.BytesIO()
        monkeypatch.setattr(cli, "_tty_out", lambda: buf)
        assert cli.emit_iterm(b"\x89PNG" + os.urandom(10), cols=40) is True
        assert b"width=40" in buf.getvalue()

    def test_emit_no_tty(self, monkeypatch):
        monkeypatch.setattr(cli, "_tty_out", lambda: None)
        assert cli.emit_kitty(b"x") is False
        assert cli.emit_iterm(b"x") is False


# ---------------------------------------------------------------------------
# viewer feed
# ---------------------------------------------------------------------------

class TestViewerFeed:
    def test_send_to_viewer_appends_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(cli, "FEED_PATH", str(tmp_path / "feed.jsonl"))
        cli.send_to_viewer("e^{i\\pi}", "dark", 300)
        lines = (tmp_path / "feed.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        item = json.loads(lines[0])
        assert item["tex"] == "e^{i\\pi}"
        assert item["theme"] == "dark"
        assert item["dpi"] == 300

    def test_send_to_viewer_with_cols(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(cli, "FEED_PATH", str(tmp_path / "feed.jsonl"))
        cli.send_to_viewer("x", "auto", 280, cols=30)
        item = json.loads((tmp_path / "feed.jsonl").read_text().strip())
        assert item["cols"] == 30


# ---------------------------------------------------------------------------
# markdown file mode
# ---------------------------------------------------------------------------

class TestMarkdownMode:
    def test_strip_fence(self):
        block = "```math\nE = mc^2\n```"
        assert cli._strip_math_block(block) == "E = mc^2"

    def test_strip_passthrough(self):
        assert cli._strip_math_block("$$x$$") == "$$x$$"

    def test_md_math_re_matches_dollars(self):
        assert cli.MD_MATH_RE.fullmatch("$$ x^2 $$") is not None

    def test_md_math_re_matches_brackets(self):
        assert cli.MD_MATH_RE.fullmatch("\\[ x^2 \\]") is not None

    def test_md_math_re_matches_fence(self):
        assert cli.MD_MATH_RE.fullmatch("```math\nx^2\n```") is not None

    def test_render_file_unicode_tier_passes_prose(self, tmp_path, capsys):
        md = tmp_path / "notes.md"
        md.write_text("Hello world\n\n$$\nx^2\n$$\n\ndone\n")
        rc = cli.render_file(make_args(file=str(md), unicode=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Hello world" in out
        assert "done" in out

    def test_render_file_missing(self, tmp_path, capsys):
        rc = cli.render_file(make_args(file=str(tmp_path / "nope.md")))
        assert rc == 0
        assert "cannot read" in capsys.readouterr().err

    @pytest.mark.skipif(not HAS_TEX, reason="latex/dvipng not installed")
    def test_render_file_pixel_path(self, tmp_path, capsys):
        md = tmp_path / "notes.md"
        md.write_text("$$\nx=1\n$$\n")
        rc = cli.render_file(make_args(file=str(md)))
        assert rc == 0


# ---------------------------------------------------------------------------
# dpi validation
# ---------------------------------------------------------------------------

class TestDpiArg:
    @pytest.mark.parametrize("value", ["50", "280", "1200"])
    def test_valid(self, value):
        assert cli._dpi_arg(value) == int(value)

    @pytest.mark.parametrize("value", ["0", "49", "1201", "5000", "abc", ""])
    def test_invalid(self, value):
        with pytest.raises(Exception):
            cli._dpi_arg(value)


# ---------------------------------------------------------------------------
# CLI end-to-end (no-latex paths)
# ---------------------------------------------------------------------------

class TestCli:
    def test_version(self, capsys):
        with pytest.raises(SystemExit) as e:
            cli.main(["-V"])
        assert e.value.code == 0
        assert "texcat 0.4.1" in capsys.readouterr().out

    def test_unicode_mode(self, capsys):
        rc = cli.main(["-u", "x^2"])
        assert rc == 0
        assert capsys.readouterr().out.strip()

    def test_file_mode_dispatch(self, tmp_path, capsys):
        md = tmp_path / "n.md"
        md.write_text("$$\nx^2\n$$\n")
        rc = cli.main(["-u", "-f", str(md)])
        assert rc == 0

    def test_bad_dpi_exits_2(self, capsys):
        with pytest.raises(SystemExit) as e:
            cli.main(["-d", "10", "x"])
        assert e.value.code == 2

    def test_no_input_errors(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        with pytest.raises(SystemExit) as e:
            cli.main([])
        assert e.value.code == 2

    def test_border_reaches_render_png(self, monkeypatch, capsys):
        captured = {}
        def fake_render_png(body, dpi, fg, bg, border=4):
            captured["border"] = border
            return b"\x89PNG\x00\x00"
        monkeypatch.setattr(cli, "render_png", fake_render_png)
        monkeypatch.setattr(cli, "graphics_protocol", lambda: "kitty")
        monkeypatch.setattr(cli, "_tty_out", lambda: io.BytesIO())
        rc = cli.main(["-b", "12", "x^2"])
        assert rc == 0
        assert captured["border"] == 12
