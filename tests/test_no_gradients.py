"""Градиенты в дизайне запрещены: ни в CSS, ни в HTML, ни в SVG приложения."""
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
PATTERNS = ("gradient(", "<lineargradient", "<radialgradient", "<conicgradient")


def test_static_files_have_no_gradients():
    found = []
    for path in sorted(STATIC.rglob("*")):
        if not path.is_file() or path.suffix not in {".css", ".html", ".svg", ".js"}:
            continue
        text = path.read_text(encoding="utf-8").lower()
        for pattern in PATTERNS:
            if pattern in text:
                found.append(f"{path.relative_to(STATIC)}: {pattern}")
    assert not found, "Градиенты найдены: " + ", ".join(found)


def test_fonts_are_self_hosted_and_licensed():
    fonts = STATIC / "fonts"
    assert list(fonts.glob("*.woff2")), "нет woff2"
    assert (fonts / "OFL-Onest.txt").is_file() and (fonts / "OFL-Alegreya.txt").is_file()
    css = (STATIC / "design.css").read_text(encoding="utf-8")
    assert "http://" not in css and "https://" not in css, "шрифты только свои"
    families = {line.split('font-family:"')[1].split('"')[0] for line in css.splitlines() if 'font-family:"' in line and "@font-face" in line}
    assert families == {"Alegreya", "Onest"}


def test_no_inline_styles_in_landing():
    html = (STATIC / "index.html").read_text(encoding="utf-8").lower()
    assert "<style" not in html and ' style="' not in html
