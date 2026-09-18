"""API keys in the operating system's credential store instead of work/settings.json.

Uses the optional `keyring` package (`pip install remediate[keyring]`): Windows Credential Manager,
macOS Keychain, or Secret Service (GNOME Keyring / KWallet) on Linux. When the package is missing
or the machine has no usable store (a headless server, a container), `available()` is False and
the web UI keeps the keys in work/settings.json as before.
"""

from __future__ import annotations

from typing import Any

SERVICE = "remediate"
# The settings that are secrets. Each is stored as one credential: service "remediate", user = name.
SECRET_KEYS = ("anthropic_api_key", "openai_api_key")


def _backend() -> Any | None:
    """The keyring module when it can really store something, else None. Tests replace this."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
    except Exception:  # a broken backend must not take the UI down
        return None
    return keyring


def available() -> bool:
    return _backend() is not None


def get_secret(name: str) -> str:
    kr = _backend()
    if kr is None:
        return ""
    try:
        return kr.get_password(SERVICE, name) or ""
    except Exception:  # locked or unreachable store: behave as if no key is saved
        return ""


def set_secret(name: str, value: str) -> bool:
    """Store `value` (an empty value deletes the entry). False when the store could not be written."""
    kr = _backend()
    if kr is None:
        return False
    try:
        if value:
            kr.set_password(SERVICE, name, value)
        elif kr.get_password(SERVICE, name) is not None:
            kr.delete_password(SERVICE, name)
        return True
    except Exception:
        return False
