from __future__ import annotations

import pytest

from workerbee.runtime_provider import validate_installed_ae_providers


def test_source_checkout_without_distribution_metadata_is_compatible() -> None:
    assert validate_installed_ae_providers([]) == ()


def test_exact_k1s_core_provider_is_compatible() -> None:
    assert validate_installed_ae_providers(["k1s_core"]) == ("k1s-core",)


def test_legacy_provider_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="active release lane requires only k1s-core"):
        validate_installed_ae_providers(["k1s-workerbee-runtime"])


def test_mixed_ae_providers_fail_closed() -> None:
    with pytest.raises(RuntimeError, match="multiple distributions provide 'ae'"):
        validate_installed_ae_providers(["k1s-core", "k1s-workerbee-runtime"])


def test_unapproved_ae_provider_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="unapproved distribution provides 'ae'"):
        validate_installed_ae_providers(["future-provider"])
