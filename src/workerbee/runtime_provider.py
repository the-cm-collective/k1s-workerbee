"""Runtime provider compatibility contract.

WorkerBee accepts one exact ``k1s-core`` distribution as the provider of the
existing ``ae`` import surface. Keep this module small: runtime discovery
imports it before any k1s process is launched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

RUNTIME_DISTRIBUTION = "k1s-core"
RUNTIME_VERSION = "1.0.0rc2"
RUNTIME_REQUIREMENT = f"{RUNTIME_DISTRIBUTION}=={RUNTIME_VERSION}"
RUNTIME_ARTIFACT_FILENAME = "k1s_core-1.0.0rc2-py3-none-any.whl"
RUNTIME_ARTIFACT_SIZE = 1057577
RUNTIME_ARTIFACT_SHA256 = "2affbd3fbb086a847a01327c29c77c582b37d44b447e689f8b2ed9e5a5480699"
LEGACY_RUNTIME_DISTRIBUTION = "k1s-workerbee-runtime"
RUNTIME_IMPORT_PACKAGE = "ae"
RUNTIME_PROVIDER_STATUS_SCHEMA = "workerbee.runtime-provider-status/v1"


class RuntimeProviderError(RuntimeError):
    """Fail-closed provider error carrying a safe structured diagnostic."""

    def __init__(self, message: str, *, status: dict[str, Any]) -> None:
        super().__init__(message)
        self.status = status


def normalize_distribution_name(name: str) -> str:
    """Return the PEP 503 normalized spelling for a distribution name."""

    return re.sub(r"[-_.]+", "-", name).lower()


def validate_installed_ae_providers(providers: Iterable[str]) -> tuple[str, ...]:
    """Reject mixed or unapproved distributions that advertise ``ae``.

    An empty provider list is valid for an explicitly selected source checkout,
    which supplies ``ae`` through ``PYTHONPATH`` instead of installed metadata.
    """

    status = runtime_provider_status(providers)
    normalized = tuple(status["providers"])
    if status["state"] == "mixed":
        joined = ", ".join(normalized)
        raise RuntimeProviderError(
            f"multiple distributions provide {RUNTIME_IMPORT_PACKAGE!r}: {joined}; "
            "remove the mixed runtime providers before starting WorkerBee",
            status=status,
        )
    if status["state"] in {"legacy", "unapproved"}:
        raise RuntimeProviderError(
            f"unapproved distribution provides {RUNTIME_IMPORT_PACKAGE!r}: "
            f"{normalized[0]}; the active release lane requires only "
            f"{normalize_distribution_name(RUNTIME_DISTRIBUTION)}",
            status=status,
        )
    return normalized


def runtime_provider_status(providers: Iterable[str]) -> dict[str, Any]:
    """Classify installed ownership of the compatibility ``ae`` namespace."""

    normalized = tuple(
        sorted({normalize_distribution_name(provider) for provider in providers if provider})
    )
    expected = normalize_distribution_name(RUNTIME_DISTRIBUTION)
    legacy = normalize_distribution_name(LEGACY_RUNTIME_DISTRIBUTION)
    if not normalized:
        state = "source-checkout"
        ok = True
    elif len(normalized) > 1:
        state = "mixed"
        ok = False
    elif normalized == (expected,):
        state = "exact"
        ok = True
    elif normalized == (legacy,):
        state = "legacy"
        ok = False
    else:
        state = "unapproved"
        ok = False
    return {
        "schema": RUNTIME_PROVIDER_STATUS_SCHEMA,
        "state": state,
        "ok": ok,
        "providers": list(normalized),
        "expected": expected,
        "forbidden": legacy,
        "import_package": RUNTIME_IMPORT_PACKAGE,
        "requirement": RUNTIME_REQUIREMENT,
    }
