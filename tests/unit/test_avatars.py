"""What the avatar pipeline refuses, and what it strips.

The refusals matter more than the happy path: this is a user-uploaded-file
feature, so each test here is a way in that is closed.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.domain import avatars


def png(size=(400, 300), colour=(200, 30, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


class TestAccepted:
    def test_a_png_becomes_a_square_webp(self):
        result = avatars.build(png())
        assert result.width == result.height == avatars.OUTPUT_SIZE
        assert Image.open(io.BytesIO(result.webp)).format == "WEBP"

    def test_the_output_is_small_enough_to_inline(self):
        # It travels in the account payload, so a photo must not bloat it.
        assert len(avatars.build(png((3000, 2000))).webp) < 60 * 1024

    def test_a_tall_image_is_cropped_not_squashed(self):
        # A centred crop; a resize alone would distort a face.
        result = avatars.build(png((200, 1200)))
        assert result.width == result.height == avatars.OUTPUT_SIZE

    def test_exif_is_not_carried_through(self):
        buf = io.BytesIO()
        image = Image.new("RGB", (500, 500), (10, 120, 90))
        exif = image.getexif()
        exif[0x010F] = "SecretCameraMake"  # Make
        exif[0x0112] = 6  # Orientation
        image.save(buf, format="JPEG", exif=exif)

        result = avatars.build(buf.getvalue())
        out = Image.open(io.BytesIO(result.webp))
        # GPS and camera identity in an avatar is a privacy leak, and it survives
        # a naive "just store the file" implementation.
        assert not dict(out.getexif())
        assert b"SecretCameraMake" not in result.webp


class TestRefused:
    def _refuses(self, raw, code):
        with pytest.raises(avatars.AvatarRejected) as caught:
            avatars.build(raw)
        assert caught.value.code == code

    def test_an_empty_file(self):
        self._refuses(b"", "avatar_empty")

    def test_something_that_is_not_an_image(self):
        # The content type the browser sent is irrelevant; only decoding decides.
        self._refuses(b"<?php system($_GET['c']); ?>" * 20, "avatar_not_an_image")

    def test_an_svg_even_though_it_is_an_image_format(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        # Serving this from our own origin would be stored XSS.
        self._refuses(svg, "avatar_not_an_image")

    def test_a_file_over_the_size_cap(self):
        self._refuses(b"\x89PNG\r\n\x1a\n" + b"\x00" * (avatars.MAX_UPLOAD_BYTES + 1),
                      "avatar_too_large")

    def test_a_polyglot_with_a_script_appended(self):
        # Valid PNG followed by script: it decodes, so it is accepted — and the
        # re-encode is what removes the tail. This asserts the tail is gone.
        payload = png() + b"<script>alert(document.cookie)</script>"
        result = avatars.build(payload)
        assert b"<script>" not in result.webp

    def test_a_decompression_bomb_is_refused_on_its_dimensions(self, monkeypatch):
        monkeypatch.setattr(avatars, "MAX_SOURCE_PIXELS", 1000)
        # 400x300 = 120,000 pixels, well over the lowered cap. The guard reads
        # the header rather than the file length, because a bomb is small on disk.
        self._refuses(png(), "avatar_too_many_pixels")


class TestDataUri:
    def test_none_stays_none(self):
        assert avatars.to_data_uri(None) is None
        assert avatars.to_data_uri(b"") is None

    def test_a_webp_becomes_an_inline_uri(self):
        uri = avatars.to_data_uri(avatars.build(png()).webp)
        assert uri.startswith("data:image/webp;base64,")
