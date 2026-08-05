# Contributing to texcat

Yes please! The whole tool is one file (`texcat/cli.py`), tests live in
`tests/`, and CI is plain pytest on ubuntu with texlive installed.

## Setup

```bash
git clone https://github.com/a-hiremath/texcat && cd texcat
pip install -e . && pip install pytest pillow
pytest -q
```

You'll want a TeX distribution (`latex` + `dvipng` on PATH) for the
integration tests; without one they auto-skip.

## Good first issues

- Sixel backend for terminals without Kitty/iTerm2 graphics
- Linux `--viewer` (spawn a window on common terminal emulators)
- Windows Terminal support
- Better Unicode-tier matrix handling (embedded matrices mid-expression)

## Ground rules

- Keep the zero-hard-dependency philosophy: Pillow, TeXicode, sympy are
  all optional enhancements; `texcat` must degrade gracefully without them.
- Every render-pipeline change bumps `_RENDER_REV` (it keys the PNG cache).
- Test with both a light and dark terminal theme before submitting
  display changes.
