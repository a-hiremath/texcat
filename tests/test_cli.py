import io
import shutil
import struct
import zlib

import pytest

from texcat import cli


# ---------------------------------------------------------------- normalize

def test_normalize_wraps_display():
    assert cli.normalize("x+1", inline=False) == "\\[\nx+1\n\\]"


def test_normalize_inline():
    assert cli.normalize("x+1", inline=True) == "$x+1$"


@pytest.mark.parametrize("wrapped", [
    "$$x+1$$", "\\[x+1\\]", "$x+1$", "\\(x+1\\)",
])
def test_normalize_strips_existing_delimiters(wrapped):
    assert cli.normalize(wrapped, inline=False) == "\\[\nx+1\n\\]"


def test_normalize_keeps_top_level_envs_bare():
    src = "\\begin{align} a &= b \\end{align}"
    assert cli.normalize(src, inline=False) == src


# ------------------------------------------------------------------ packages

def test_extra_packages_parses_and_filters(monkeypatch):
    monkeypatch.delenv("TEXCAT_PACKAGES", raising=False)
    assert cli.extra_packages("physics, siunitx") == ["physics", "siunitx"]
    # injection attempts and junk are dropped
    assert cli.extra_packages("phys}\\evil{,ok-pkg") == ["ok-pkg"]


def test_extra_packages_env(monkeypatch):
    monkeypatch.setenv("TEXCAT_PACKAGES", "bm")
    assert cli.extra_packages(None) == ["bm"]


def test_build_doc_dedupes_base_packages():
    doc, preamble = cli.build_doc("x", 4, ["amsmath", "physics"])
    assert preamble.count("amsmath") == 1
    assert "\\usepackage{physics}" in preamble
    assert doc.endswith("\\end{document}\n")


# ---------------------------------------------------------------- png helpers

def _mini_png(w=10, h=6):
    def chunk(tag, data):
        raw = tag + data
        return (struct.pack(">I", len(data)) + raw
                + struct.pack(">I", zlib.crc32(raw)))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    row = b"\x00" + b"\xff\x00\x00\xff" * w
    idat = zlib.compress(row * h)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def test_png_size_reads_ihdr():
    assert cli.png_size(_mini_png(123, 45)) == (123, 45)


def test_png_size_garbage():
    assert cli.png_size(b"not a png") == (0, 0)


def test_autocrop_opaque_card():
    PIL = pytest.importorskip("PIL")
    from PIL import Image

    im = Image.new("RGB", (300, 100), "white")
    for x in range(140, 160):
        for y in range(45, 55):
            im.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    cropped = cli._autocrop(buf.getvalue(), pad=5)
    w, h = cli.png_size(cropped)
    assert w == 20 + 10 and h == 10 + 10  # ink + 2*pad


# ------------------------------------------------------------- markdown split

def test_md_math_re_finds_all_block_styles():
    text = ("intro\n$$a+b$$\nmiddle\n\\[c \\cdot d\\]\n"
            "```math\ne=f\n```\ntail\n")
    blocks = [p for p in cli.MD_MATH_RE.split(text)
              if cli.MD_MATH_RE.fullmatch(p)]
    assert len(blocks) == 3
    assert cli._strip_math_block(blocks[2]) == "e=f"


# ------------------------------------------------------------ unicode helpers

def test_pmod_preprocess():
    out = cli._preprocess_unicode(r"a \pmod{p}")
    assert "pmod" not in out and "mod" in out


# -------------------------------------------------------- integration (TeX)

needs_tex = pytest.mark.skipif(
    not (shutil.which("latex") and shutil.which("dvipng")),
    reason="TeX toolchain not installed",
)


@needs_tex
def test_render_png_produces_ink(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CACHE_DIR", str(tmp_path))
    png = cli.render_png("\\[ x^2 \\]", dpi=150,
                         fg="rgb 0.0 0.0 0.0", bg="rgb 1.0 1.0 1.0")
    w, h = cli.png_size(png)
    assert w > 5 and h > 5


@needs_tex
def test_render_png_raises_on_bad_tex(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CACHE_DIR", str(tmp_path))
    with pytest.raises(cli.TexError):
        cli.render_png("\\[ \\frac{unclosed \\]", dpi=150,
                       fg="rgb 0.0 0.0 0.0", bg="rgb 1.0 1.0 1.0")


# ------------------------------------------------------- v0.9.0 unicode tier

def test_rewrites_implies_iff_bigcup():
    out = cli._preprocess_unicode(r"A \implies B \iff \bigcup_{i} C_i")
    assert "\\Rightarrow" in out and "\\Leftrightarrow" in out
    assert "\\cup_{i}" in out and "bigcup" not in out


def test_rewrite_boundary_does_not_eat_longer_names():
    # \implies must not rewrite inside \impliesfoo (hypothetical macro)
    assert "\\Rightarrow" not in cli._preprocess_unicode(r"\impliesfoo")


def test_hstack_centers_blocks():
    out = cli._hstack([["A ="], ["1", "2", "3"]])
    lines = out.split("\n")
    assert len(lines) == 3
    assert "A =" in lines[1]  # vertically centered


needs_txc = pytest.mark.skipif(
    not shutil.which("txc"), reason="TeXicode not installed"
)


@needs_txc
def test_txc_try_detects_stdout_errors():
    # txc reports errors on stdout with rc=0 — must read as failure
    assert cli._txc_try(r"A = \begin{bmatrix} 1 \\ 2 \end{bmatrix}") is None
    assert cli._txc_try("x + 1") is not None


@needs_txc
def test_txc_try_leading_minus():
    assert cli._txc_try("-x") is not None


@needs_txc
def test_unicode_cases_renders_brace():
    out = cli._unicode_cases(
        r"|x| = \begin{cases} x & x \ge 0 \\ -x & x < 0 \end{cases}")
    assert out is not None and "⎧" in out and "⎩" in out


@needs_txc
def test_unicode_matrix_embedded():
    pytest.importorskip("sympy")
    out = cli._unicode_matrix(
        r"\det \begin{bmatrix} a & b \\ c & d \end{bmatrix} = ad - bc")
    assert out is not None and "⎡" in out and "=" in out
