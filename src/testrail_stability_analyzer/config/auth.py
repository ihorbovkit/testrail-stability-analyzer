"""TestRail credential providers.

Credentials are obtained on demand from an approved source and are never part
of :class:`~.settings.Settings`. Built in: environment variables and the macOS
Keychain. Other sources can be added by implementing :class:`CredentialProvider`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from .settings import CredentialProviderSettings

DEFAULT_USERNAME_VARIABLE = "TESTRAIL_USERNAME"
DEFAULT_API_KEY_VARIABLE = "TESTRAIL_API_KEY"

_REDACTED = "<redacted>"

SECURITY_EXECUTABLE: Final = "/usr/bin/security"
_SECURITY_ITEM_NOT_FOUND: Final = 44  # errSecItemNotFound, as reported by the security tool
_SECURITY_TIMEOUT_SECONDS: Final = 60.0  # allows time to answer a Keychain access prompt


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


class KeychainCredentialProvider:
    """Reads the API key from a generic password item in the macOS Keychain.

    The item is looked up by service and account name (both configured
    locally; there are no defaults). The account name is the TestRail
    username unless ``username_variable`` names an environment variable that
    holds it. Uses the system ``security`` tool read-only; the secret is read
    from its standard output and is never passed as an argument, logged, or
    included in errors.
    """

    def __init__(
        self,
        service: str,
        account: str,
        *,
        username_variable: str | None = None,
        environ: Mapping[str, str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        platform: str = sys.platform,
    ) -> None:
        if not service or not account:
            raise CredentialError("macOS Keychain service and account names must be configured")
        self._service = service
        self._account = account
        self._username_variable = username_variable
        self._environ = environ
        self._runner = runner
        self._platform = platform

    def get_credentials(self) -> Credentials:
        if self._platform != "darwin":
            raise CredentialError("the macOS Keychain credential provider is only available on macOS")
        # The username is resolved first so that a missing variable never triggers a Keychain read.
        username = self._username()
        return Credentials(username=username, api_key=self._read_password())

    def _read_password(self) -> str:
        command = [SECURITY_EXECUTABLE, "find-generic-password", "-s", self._service, "-a", self._account, "-w"]
        options: dict[str, Any] = {"capture_output": True, "text": True, "check": False}
        try:
            completed = self._runner(command, timeout=_SECURITY_TIMEOUT_SECONDS, **options)
        except FileNotFoundError:
            raise CredentialError(f"macOS Keychain tool not found at {SECURITY_EXECUTABLE}") from None
        except subprocess.TimeoutExpired:
            raise CredentialError("timed out reading the TestRail API key from the macOS Keychain") from None
        except OSError as exc:
            raise CredentialError(f"could not run the macOS Keychain tool ({type(exc).__name__})") from None
        # Tool output is never included in errors: stdout may hold the secret.
        if completed.returncode == _SECURITY_ITEM_NOT_FOUND:
            raise CredentialError(
                f"no macOS Keychain item found for service {self._service!r} and the configured account"
            )
        if completed.returncode != 0:
            raise CredentialError(
                f"could not read the macOS Keychain item for service {self._service!r} "
                f"(security exit status {completed.returncode}); access may have been denied or cancelled"
            )
        api_key = (completed.stdout or "").strip()
        if not api_key:
            raise CredentialError(f"the macOS Keychain item for service {self._service!r} has an empty password")
        return api_key

    def _username(self) -> str:
        if self._username_variable is None:
            return self._account
        environ = os.environ if self._environ is None else self._environ
        username = (environ.get(self._username_variable) or "").strip()
        if not username:
            raise CredentialError(
                f"TestRail username not set; missing environment variable(s): {self._username_variable}"
            )
        return username

    def __repr__(self) -> str:
        return f"KeychainCredentialProvider(service={self._service!r}, account={_REDACTED})"


def credential_provider_from_settings(
    settings: CredentialProviderSettings, *, environ: Mapping[str, str] | None = None
) -> CredentialProvider:
    """The provider selected in ``[credential_provider]``; environment variables by default."""
    if settings.type == "keychain":
        assert settings.keychain_service is not None and settings.keychain_account is not None  # validated
        return KeychainCredentialProvider(
            settings.keychain_service,
            settings.keychain_account,
            username_variable=settings.username_variable,
            environ=environ,
        )
    if settings.username_variable is not None:
        return EnvironmentCredentialProvider(username_variable=settings.username_variable, environ=environ)
    return EnvironmentCredentialProvider(environ=environ)
