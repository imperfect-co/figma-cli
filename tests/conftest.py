import pytest


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    """Point HOME at an empty directory so no test reads a real stored token."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home
