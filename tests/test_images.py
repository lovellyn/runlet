import io

import pytest
from PIL import Image, TiffImagePlugin

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


def test_preview_does_not_invoke_decoders_for_unsupported_formats(tmp_path, monkeypatch):
    path = tmp_path / "unsupported.tiff"
    Image.new("RGB", (10, 10), "red").save(path, format="TIFF")

    def forbidden_decoder(_self):
        raise AssertionError("The TIFF decoder must not run for an image preview")

    monkeypatch.setattr(TiffImagePlugin.TiffImageFile, "_open", forbidden_decoder)
    with pytest.raises(SandboxError):
        preview(path, path.name, 20)


def test_preview_preserves_transparency(tmp_path):
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (80, 40), (20, 40, 60, 128)).save(path)
    result = preview(path, path.name, 20)
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.mode == "RGBA"
        assert image.getchannel("A").getextrema() == (128, 128)


def test_preview_applies_exif_orientation_before_resizing(tmp_path):
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6  # Rotate 90 degrees clockwise.
    Image.new("RGB", (80, 40), "red").save(path, exif=exif)
    result = preview(path, path.name, 20)
    assert (result.metadata["original_width"], result.metadata["original_height"]) == (80, 40)
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.size == (10, 20)
        assert not image.getexif()


def test_animated_preview_uses_only_the_first_frame(tmp_path):
    path = tmp_path / "animated.gif"
    first = Image.new("RGB", (80, 40), "red")
    first.save(path, save_all=True, append_images=[Image.new("RGB", first.size, "blue")])
    result = preview(path, path.name, 20)
    assert result.metadata["first_frame_only"]
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)
        assert getattr(image, "n_frames", 1) == 1
