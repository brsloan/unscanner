"""Shared fixtures. Tests never touch the real OS credential store."""

from __future__ import annotations

import pytest

from remediate import keystore


class MemoryKeyring:
    """Stands in for the `keyring` module: same three calls, kept in a dict."""

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        del self.items[(service, name)]


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    """By default behave like a machine without a credential store."""
    monkeypatch.setattr(keystore, "_backend", lambda: None)


@pytest.fixture
def memory_keyring(monkeypatch, no_keyring):
    kr = MemoryKeyring()
    monkeypatch.setattr(keystore, "_backend", lambda: kr)
    return kr
