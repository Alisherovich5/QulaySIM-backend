"""Turning an uploaded file into an avatar we are willing to store and serve.

User-uploaded images are one of the classic ways into an application, so nothing
about the upload is taken on trust:

* **The declared content type is ignored.** Anyone can send
  `Content-Type: image/png` with a PHP script in the body. The only thing that
  establishes an upload is an image is decoding it.
* **The bytes are never stored.** The image is re-encoded to WebP, which is what
  removes EXIF (including GPS), colour profiles, trailing data after the image,
  and anything hidden in a chunk a viewer might interpret. A file that is both a
  valid image and something else stops being that something else here.
* **SVG is refused outright.** It is a document, not a raster: it can carry
  script, and serving one from our own origin would be stored XSS.
* **Decoded size is bounded before decoding.** A 40 KB file can expand to
  gigabytes of pixels; the guard is on the dimensions in the header, not on the
  file length.
* **Dimensions are capped and the result is square.** A one-pixel-wide image
  10,000 tall is not an avatar, and cropping here means the front end never has
  to.

The output is deliberately small — a 256px square is more than any avatar slot
needs — so the bytes are cheap to keep in the database and to send inline.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

# 5 MB in, a few kilobytes out. The cap exists so a request cannot tie up memory
# before the pixel check runs.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# Guards against a decompression bomb: a small file declaring an enormous canvas.
MAX_SOURCE_PIXELS = 50_000_000
OUTPUT_SIZE = 256
WEBP_QUALITY = 82

# Formats we are willing to decode. Anything else — SVG, PDF, a video container
# Pillow happens to open — is refused rather than converted.
ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "GIF", "BMP", "HEIF", "HEIC"})


class AvatarRejectedError(ValueError):
    """The upload is not something we will store. The message is user-facing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Avatar:
    webp: bytes
    width: int
    height: int


def build(raw: bytes) -> Avatar:
    """Validate, crop and re-encode an uploaded image. Raises AvatarRejectedError."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not raw:
        raise AvatarRejectedError("avatar_empty", "The file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AvatarRejectedError(
            "avatar_too_large",
            f"That image is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB.",
        )

    try:
        probe = Image.open(io.BytesIO(raw))
    except UnidentifiedImageError:
        raise AvatarRejectedError(
            "avatar_not_an_image", "That file is not an image we can read."
        ) from None
    except Exception:  # noqa: BLE001 — see below
        # Deliberately blind. Pillow raises whatever its individual format
        # parsers happen to raise on malformed input — the set is undocumented
        # and grows between releases — and this function's whole job is to make
        # sure a hostile or simply broken upload becomes a 400 rather than a
        # 500. Narrowing this to a list of exception types would mean the next
        # Pillow version quietly starts returning server errors.
        raise AvatarRejectedError(
            "avatar_not_an_image", "That file is not an image we can read."
        ) from None

    fmt = (probe.format or "").upper()
    if fmt not in ALLOWED_FORMATS:
        raise AvatarRejectedError(
            "avatar_bad_format",
            "Please upload a JPEG, PNG or WebP image.",
        )

    # Read from the header, before any pixels are allocated.
    width, height = probe.size
    if width < 1 or height < 1:
        raise AvatarRejectedError("avatar_not_an_image", "That image has no content.")
    if width * height > MAX_SOURCE_PIXELS:
        raise AvatarRejectedError(
            "avatar_too_many_pixels", "That image is too large to process."
        )

    try:
        probe.load()
        # Honour the EXIF orientation flag before dropping EXIF, or a phone photo
        # ends up rotated.
        image = ImageOps.exif_transpose(probe)
        # A centred square crop, then resize. `fit` does both and keeps the
        # subject centred, which is what a circular avatar frame needs.
        image = ImageOps.fit(
            image.convert("RGB"), (OUTPUT_SIZE, OUTPUT_SIZE), method=Image.Resampling.LANCZOS
        )
    except Exception:  # noqa: BLE001 — same reasoning as the probe above
        raise AvatarRejectedError(
            "avatar_unreadable", "That image could not be processed. Try another."
        ) from None

    buffer = io.BytesIO()
    # A fresh encode from pixel data: nothing from the original file survives.
    image.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=4)
    return Avatar(webp=buffer.getvalue(), width=OUTPUT_SIZE, height=OUTPUT_SIZE)


def to_data_uri(webp: bytes | None) -> str | None:
    """Inline form for the account payload.

    Sent inline rather than served from a URL so there is no public endpoint to
    enumerate and no authenticated image request for an <img> tag to fail at —
    the access token lives in memory, so it cannot ride along on one.
    """
    if not webp:
        return None
    import base64

    return "data:image/webp;base64," + base64.b64encode(webp).decode("ascii")
