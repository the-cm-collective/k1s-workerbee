from __future__ import annotations

import pytest

from workerbee.runtime_provider import validate_installed_ae_providers


def test_source_checkout_without_distribution_metadata_is_compatible() -> None:
    assert validate_installed_ae_providers([]) == ()


def test_frozen_legacy_provider_is_compatible() -> None:
    assert validate_installed_ae_providers(["k1s_workerbee_runtime"]) == ("k1s-workerbee-runtime",)


def test_mixed_ae_providers_fail_closed() -> None:
    with pytest.raises(RuntimeError, match="multiple distributions provide 'ae'"):
        validate_installed_ae_providers(["k1s-workerbee-runtime", "future-provider"])


def test_unapproved_ae_provider_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="unapproved distribution provides 'ae'"):
        validate_installed_ae_providers(["future-provider"])
