import sys

import pytest

from runlet.git_auth import askpass


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Username for 'https://github.com':", "x-access-token\n"),
        ("Password for 'https://x-access-token@github.com':", "fake-token-for-test\n"),
        ("Password for 'https://github.com.evil.invalid':", ""),
        ("Password for 'https://example.invalid':", ""),
    ],
)
def test_askpass_only_supplies_credentials_for_github(prompt, expected, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")
    monkeypatch.setattr(sys, "argv", ["runlet-askpass", prompt])
    askpass()
    assert capsys.readouterr().out == expected
