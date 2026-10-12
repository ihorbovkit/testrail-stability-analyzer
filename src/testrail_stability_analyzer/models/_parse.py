"""Helpers for reading typed values from TestRail API objects."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

CUSTOM_FIELD_PREFIX = "custom_"
_COUNT_SUFFIX = "_count"


class ModelError(ValueError):
    """An API object is malformed (wrong type or missing required field)."""


def frozen(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(mapping))


def to_utc(timestamp: int) -> datetime:
    """Convert a TestRail Unix timestamp (seconds) to an aware UTC datetime."""
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise ModelError(f"timestamp out of range: {timestamp}") from None


class Fields:
    """Reads typed values from one API object and tracks which keys were consumed.

    Keys that are not read explicitly end up in :meth:`custom_fields` (``custom_*``)
    or :meth:`extra` (everything else), so unknown fields are preserved.
    """

    def __init__(self, data: object, entity: str) -> None:
        if not isinstance(data, Mapping):
            raise ModelError(f"{entity}: expected an object, got {type(data).__name__}")
        self._data: Mapping[str, Any] = data
        self._entity = entity
        self._used: set[str] = set()

    def _get(self, key: str) -> Any:
        self._used.add(key)
        return self._data.get(key)

    def _error(self, key: str, expected: str) -> ModelError:
        value = self._data.get(key)
        return ModelError(f"{self._entity}.{key}: expected {expected}, got {type(value).__name__}")

    def id(self, key: str = "id") -> int:
        value = self.integer(key)
        if value is None:
            raise ModelError(f"{self._entity}.{key} is required")
        return value

    def integer(self, key: str) -> int | None:
        value = self._get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._error(key, "an integer")
        return value

    def string(self, key: str) -> str | None:
        value = self._get(key)
        if value is None or isinstance(value, str):
            return value
        raise self._error(key, "a string")

    def required_string(self, key: str) -> str:
        value = self.string(key)
        if value is None:
            raise ModelError(f"{self._entity}.{key} is required")
        return value

    def boolean(self, key: str) -> bool | None:
        value = self._get(key)
        if value is None or isinstance(value, bool):
            return value
        if value in (0, 1) and isinstance(value, int):  # some older endpoints use 0/1
            return bool(value)
        raise self._error(key, "a boolean")

    def timestamp(self, key: str) -> datetime | None:
        value = self.integer(key)
        return None if value is None else to_utc(value)

    def int_list(self, key: str) -> tuple[int, ...]:
        value = self._get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int) for v in value):
            raise self._error(key, "a list of integers")
        return tuple(value)

    def array(self, key: str) -> list[Any]:
        value = self._get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            raise self._error(key, "a list")
        return value

    def mapping(self, key: str) -> Mapping[str, Any]:
        value = self._get(key)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise self._error(key, "an object")
        return value

    def raw(self, key: str) -> Any:
        return self._get(key)

    def status_counts(self) -> Mapping[str, int]:
        """``<status>_count`` keys (incl. ``custom_statusN_count``) keyed by ``<status>``."""
        counts = {}
        for key in self._data:
            if key.endswith(_COUNT_SUFFIX) and key not in self._used:
                value = self.integer(key)
                if value is not None:
                    counts[key.removesuffix(_COUNT_SUFFIX)] = value
        return frozen(counts)

    def custom_fields(self) -> Mapping[str, Any]:
        """Remaining ``custom_*`` keys, keyed by their full system name."""
        values = {}
        for key in self._data:
            if key.startswith(CUSTOM_FIELD_PREFIX) and key not in self._used:
                values[key] = self._get(key)
        return frozen(values)

    def extra(self) -> Mapping[str, Any]:
        """All keys not consumed so far."""
        return frozen({key: value for key, value in self._data.items() if key not in self._used})
