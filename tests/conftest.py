import pytest


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    # Tests never reach a real API; translation stays off unless a test turns it on.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
