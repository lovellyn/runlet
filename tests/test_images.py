import io

import pytest
from PIL import Image

from runlet.errors import SandboxError
from runlet.images import MAX_IMAGE_BYTES, preview


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "GIF"])
def test_image_formats_resize_and_strip_metadata(tmp_path, format):
    path = tmp_path / "image"
    source = Image.new("RGB", (80, 40), "red")
    source.save(path, format=format)
    result = preview(path, "image", 20)
    assert result.metadata["source_format"] == format
    with Image.open(io.BytesIO(result.data)) as decoded:
        assert decoded.size == (20, 10)
        assert not decoded.getexif()
    assert result.mime_type == ("image/jpeg" if format == "JPEG" else "image/png")


def test_image_input_bound_and_invalid_data(tmp_path):
    path = tmp_path / "image"
    path.write_bytes(b"not an image")
    with pytest.raises(SandboxError, match="invalid_image"):
        preview(path, "image", 20)
    with path.open("wb") as stream:
        stream.truncate(MAX_IMAGE_BYTES + 1)
    with pytest.raises(SandboxError, match="image_too_large"):
        preview(path, "image", 20)
