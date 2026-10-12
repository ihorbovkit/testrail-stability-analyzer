"""TestRail credential providers.

Credentials are obtained on demand from an approved source and are never part
of :class:`~.settings.Settings`. Additional providers (e.g. macOS Keychain)
can be added by implementing :class:`CredentialProvider`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

DEFAULT_USERNAME_VARIABLE = "TESTRAIL_USERNAME"
DEFAULT_API_KEY_VARIABLE = "TESTRAIL_API_KEY"

_REDACTED = "<redacted>"


class CredentialError(RuntimeError):
    """Raised when credentials cannot be obtained. Never contains secret values."""


@dataclass(frozen=True, slots=True)
class Credentials:
    """TestRail username and API key. Both are redacted from repr/str."""

    username: str = field(repr=False)
    api_key: str = field(repr=False)

    def __repr__(self) -> str:
        return f"Credentials(username={_REDACTED}, api_key={_REDACTED})"

    __str__ = __repr__


@runtime_checkable
class CredentialProvider(Protocol):
    def get_credentials(self) -> Credentials: ...


class EnvironmentCredentialProvider:
    """Reads credentials from environment variables at the time they are needed."""

    def __init__(
        self,
        username_variable: str = DEFAULT_USERNAME_VARIABLE,
        api_key_variable: str = DEFAULT_API_KEY_VARIABLE,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._username_variable = username_variable
        self._api_key_variable = api_key_variable
        self._environ = environ

    def get_credentials(self) -> Credentials:
        environ = os.environ if self._environ is None else self._environ
        username = (environ.get(self._username_variable) or "").strip()
        api_key = (environ.get(self._api_key_variable) or "").strip()
        missing = [
            name
            for name, value in ((self._username_variable, username), (self._api_key_variable, api_key))
            if not value
        ]
        if missing:
            raise CredentialError(f"TestRail credentials not set; missing environment variable(s): {', '.join(missing)}")
        return Credentials(username=username, api_key=api_key)

    def __repr__(self) -> str:
        return (
            f"EnvironmentCredentialProvider(username_variable={self._username_variable!r}, "
            f"api_key_variable={self._api_key_variable!r})"
        )
