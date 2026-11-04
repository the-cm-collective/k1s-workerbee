"""Runtime provider compatibility contract.

WorkerBee accepts one exact ``k1s-core`` distribution as the provider of the
existing ``ae`` import surface. Keep this module small: runtime discovery
imports it before any k1s process is launched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

RUNTIME_DISTRIBUTION = "k1s-core"
RUNTIME_VERSION = "0.1.8a1"
RUNTIME_REQUIREMENT = f"{RUNTIME_DISTRIBUTION}=={RUNTIME_VERSION}"
RUNTIME_ARTIFACT_FILENAME = "k1s_core-0.1.8a1-py3-none-any.whl"
RUNTIME_ARTIFACT_SIZE = 879766
RUNTIME_ARTIFACT_SHA256 = "11103d1df772d9618e023d40a8bdfeecb10feac7a5a374fa2efe0b1ac607cb30"
LEGACY_RUNTIME_DISTRIBUTION = "k1s-workerbee-runtime"
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
    expected = normalize_distribution_name(RUNTIME_DISTRIBUTION)
    if len(normalized) > 1:
        joined = ", ".join(normalized)
        raise RuntimeError(
            f"multiple distributions provide {RUNTIME_IMPORT_PACKAGE!r}: {joined}; "
            "remove the mixed runtime providers before starting WorkerBee"
        )
    if normalized and normalized != (expected,):
        raise RuntimeError(
            f"unapproved distribution provides {RUNTIME_IMPORT_PACKAGE!r}: "
            f"{normalized[0]}; the active release lane requires only {expected}"
        )
    return normalized
