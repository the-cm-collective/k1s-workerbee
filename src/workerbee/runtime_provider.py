"""Runtime provider compatibility contract.

The public 0.1.7 release remains on the legacy distribution while the
replacement k1s artifact is reviewed independently.  Keep this module small:
it is imported by runtime discovery before any k1s process is launched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

LEGACY_RUNTIME_DISTRIBUTION = "k1s-workerbee-runtime"
LEGACY_RUNTIME_VERSION = "0.1.7"
LEGACY_RUNTIME_REQUIREMENT = f"{LEGACY_RUNTIME_DISTRIBUTION}=={LEGACY_RUNTIME_VERSION}"
RUNTIME_IMPORT_PACKAGE = "ae"


def normalize_distribution_name(name: str) -> str:
    """Return the PEP 503 normalized spelling for a distribution name."""

    return re.sub(r"[-_.]+", "-", name).lower()


def validate_installed_ae_providers(providers: Iterable[str]) -> tuple[str, ...]:
    """Reject mixed or unapproved distributions that advertise ``ae``.

    An empty provider list is valid for an explicitly selected source checkout,
    which supplies ``ae`` through ``PYTHONPATH`` instead of installed metadata.
    """

    normalized = tuple(
        sorted({normalize_distribution_name(provider) for provider in providers if provider})
    )
    expected = normalize_distribution_name(LEGACY_RUNTIME_DISTRIBUTION)
    if len(normalized) > 1:
        joined = ", ".join(normalized)
        raise RuntimeError(
            f"multiple distributions provide {RUNTIME_IMPORT_PACKAGE!r}: {joined}; "
            "remove the mixed runtime providers before starting WorkerBee"
        )
    if normalized and normalized != (expected,):
        raise RuntimeError(
            f"unapproved distribution provides {RUNTIME_IMPORT_PACKAGE!r}: "
            f"{normalized[0]}; the active release lane requires {expected}"
        )
    return normalized
