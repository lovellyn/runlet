"""Bounded raster-image decoding and previews for MCP vision inputs."""

import io
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import SandboxError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
DEFAULT_IMAGE_DIMENSION = 1600
MAX_CONCURRENT_IMAGES = 2


@dataclass(frozen=True)
class ImagePreview:
    data: bytes
    mime_type: str
    metadata: dict


def preview(path: Path, relative: str, max_dimension: int) -> ImagePreview:
    if not 1 <= max_dimension <= MAX_IMAGE_DIMENSION:
        raise SandboxError("invalid_image_size", "max_dimension must be between 1 and 4096")
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise SandboxError("image_too_large", "Image file exceeds the 10 MiB input limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise SandboxError("unsupported_image", "Use PNG, JPEG, WebP or GIF")
                width, height = source.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise SandboxError("image_too_large", "Image exceeds the 25 megapixel limit")
                source_format = source.format
                first_frame = getattr(source, "n_frames", 1) > 1
                source.seek(0)
                image = ImageOps.exif_transpose(source)
                image.load()
                alpha = "A" in image.getbands() or "transparency" in image.info
                image = image.convert("RGBA" if alpha else "RGB")
                image.info.clear()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise SandboxError(
            "image_too_large", "Image dimensions exceed the decoding limit"
        ) from error
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        if isinstance(error, SandboxError):
            raise
        raise SandboxError("invalid_image", "Image cannot be decoded") from error

    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    output_format = "JPEG" if source_format == "JPEG" else "PNG"
    while True:
        buffer = io.BytesIO()
        if output_format == "JPEG":
            image.save(buffer, format="JPEG", quality=85)
        else:
            image.save(buffer, format="PNG")
        output = buffer.getvalue()
        if len(output) <= MAX_IMAGE_OUTPUT_BYTES:
            break
        if image.size == (1, 1):
            raise SandboxError("image_too_large", "Preview exceeds the output limit")
        image = image.resize(
            (max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)),
            Image.Resampling.LANCZOS,
        )
    mime = "image/jpeg" if output_format == "JPEG" else "image/png"
    return ImagePreview(
        output,
        mime,
        {
            "path": relative,
            "source_format": source_format,
            "original_width": width,
            "original_height": height,
            "width": image.width,
            "height": image.height,
            "mime_type": mime,
            "first_frame_only": first_frame,
            "bytes": len(output),
        },
    )
