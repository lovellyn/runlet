import pytest

from scripts.check_release import validate


@pytest.mark.parametrize("version", ["0.3.0", "1.0.0", "10.20.30"])
def test_release_tag_matches_package(version):
    validate(f"v{version}", version)


@pytest.mark.parametrize(
    "tag", ["0.3.0", "v0.3", "v0.3.0-rc.1", "v00.3.0", "v0.3.0/extra", "v0.3.0\n", "v9.9.9"]
)
def test_rejects_nonstable_or_mismatched_release_tags(tag):
    with pytest.raises(ValueError):
        validate(tag, "0.3.0")
