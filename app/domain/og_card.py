"""The share card a destination link unfurls into.

Telegram is how this audience passes links around, and Telegram shows exactly
what og:image points at. Until now that was one generic banner for every page,
so a Turkey plan shared into a group chat previewed identically to the home
page. This draws a card per destination — its name and its real entry price —
which is the difference between a link that looks like a site and a link that
looks like an answer.

Pure pixels in, bytes out: no I/O here, so it tests without a database and the
router owns caching. The palette is the storefront's dark hero (the globe
panel's gradient stops and the accent the brand uses on dark surfaces),
because the card is most often seen inside Telegram's dark theme.
"""

from __future__ import annotations

import io
from decimal import Decimal

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 630

# The storefront's dark-hero gradient stops and inks (see GLOBE_THEME.dark).
_TOP = (11, 47, 60)  # #0b2f3c
_BOTTOM = (3, 18, 26)  # #03121a
_ACCENT = (52, 227, 176)  # #34e3b0
_INK = (255, 255, 255)
_INK_SOFT = (159, 184, 193)  # muted caption ink on the dark wash

_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

_FontT = ImageFont.FreeTypeFont | ImageFont.ImageFont


def _font(size: int, *, bold: bool = True) -> _FontT:
    """DejaVu when the image ships it; Pillow's built-in scalable face otherwise.

    The container installs fonts-dejavu-core, so production always takes the
    first branch. The fallback exists for local dev and CI on machines without
    that path — a slightly different face there beats a crashed endpoint.
    """
    try:
        return ImageFont.truetype(_DEJAVU_BOLD if bold else _DEJAVU, size)
    except OSError:
        return ImageFont.load_default(size=size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: _FontT) -> float:
    return draw.textlength(text, font=font)


def render_card(country_name: str, price_usd: Decimal | None) -> bytes:
    """One 1200×630 PNG. Everything on it is true: the name is the localised
    catalogue name and the price is the cheapest plan actually on sale."""
    image = Image.new("RGB", (WIDTH, HEIGHT), _BOTTOM)
    draw = ImageDraw.Draw(image)

    # Vertical gradient, one row at a time — 630 lines is nothing, and it
    # avoids pulling in numpy for a single background.
    for y in range(HEIGHT):
        t = y / HEIGHT
        draw.line(
            [(0, y), (WIDTH, y)],
            fill=tuple(round(a + (b - a) * t) for a, b in zip(_TOP, _BOTTOM, strict=True)),
        )

    # The globe motif, reduced to two quiet rings on the right so the left
    # column of text has something to sit against. Drawn on an overlay for
    # real alpha; Pillow strokes have none of their own.
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ring = ImageDraw.Draw(overlay)
    for radius, alpha in ((300, 38), (210, 26)):
        ring.ellipse(
            (1040 - radius, 315 - radius, 1040 + radius, 315 + radius),
            outline=(*_ACCENT, alpha),
            width=3,
        )
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    # Wordmark. Two runs of one line: the brand sets "sim" in the accent.
    brand = _font(46)
    x = 72.0
    draw.text((x, 56), "Qulay", font=brand, fill=_INK)
    x += _text_width(draw, "Qulay", brand)
    draw.text((x, 56), "sim", font=brand, fill=_ACCENT)

    # Country name, shrunk until it fits rather than wrapped: a two-line name
    # in a preview thumbnail reads worse than a smaller one-line name.
    size = 116
    name_font = _font(size)
    while size > 48 and _text_width(draw, country_name, name_font) > WIDTH - 144:
        size -= 8
        name_font = _font(size)
    draw.text((72, 236), country_name, font=name_font, fill=_INK)

    # The line that earns the click. A destination with no priced plans gets
    # the plain product word instead of an invented number. The separator dot
    # is drawn, not typed: the fallback face has no U+2022 and renders a tofu
    # box, and a share card with a missing-glyph box reads as broken.
    price_font = _font(56)
    line_y = 236 + size + 34
    if price_usd is not None:
        x = 72.0
        draw.text((x, line_y), "eSIM", font=price_font, fill=_ACCENT)
        x += _text_width(draw, "eSIM", price_font) + 26
        dot_y = line_y + 34
        draw.ellipse((x, dot_y, x + 12, dot_y + 12), fill=_ACCENT)
        x += 12 + 26
        draw.text((x, line_y), f"${price_usd} dan", font=price_font, fill=_ACCENT)
    else:
        draw.text((72, line_y), "eSIM tariflari", font=price_font, fill=_ACCENT)

    draw.text((72, HEIGHT - 78), "qulaysim.uz", font=_font(30, bold=False), fill=_INK_SOFT)
    draw.rectangle((0, HEIGHT - 10, WIDTH, HEIGHT), fill=_ACCENT)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
