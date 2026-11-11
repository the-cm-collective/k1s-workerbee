from __future__ import annotations

import pytest

from workerbee import cli
from workerbee.runtime_provider import (
    RuntimeProviderError,
    runtime_provider_status,
    validate_installed_ae_providers,
)


def test_source_checkout_without_distribution_metadata_is_compatible() -> None:
    assert validate_installed_ae_providers([]) == ()


def test_exact_k1s_core_provider_is_compatible() -> None:
    assert validate_installed_ae_providers(["k1s_core"]) == ("k1s-core",)
    assert runtime_provider_status(["k1s_core"])["state"] == "exact"


def test_legacy_provider_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="active release lane requires only k1s-core"):
        validate_installed_ae_providers(["k1s-workerbee-runtime"])


def test_mixed_ae_providers_fail_closed() -> None:
    with pytest.raises(RuntimeProviderError, match="multiple distributions provide 'ae'") as error:
        validate_installed_ae_providers(["k1s-core", "k1s-workerbee-runtime"])
    assert error.value.status == {
        "schema": "workerbee.runtime-provider-status/v1",
        "state": "mixed",
        "ok": False,
        "providers": ["k1s-core", "k1s-workerbee-runtime"],
        "expected": "k1s-core",
        "forbidden": "k1s-workerbee-runtime",
        "import_package": "ae",
        "requirement": "k1s-core==1.0.0rc1",
    }


def test_unapproved_ae_provider_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="unapproved distribution provides 'ae'"):
        validate_installed_ae_providers(["future-provider"])


def test_doctor_reports_structured_mixed_provider_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    status = runtime_provider_status(["k1s-core", "k1s-workerbee-runtime"])

    def mixed_provider() -> None:
        raise RuntimeProviderError("mixed providers", status=status)

    monkeypatch.setattr(cli, "resolve_k1s_runtime", mixed_provider)
    monkeypatch.setattr(cli, "runtime_diagnostics", lambda *_args, **_kwargs: {"ok": False})
    monkeypatch.setattr(cli, "containerd_privilege_status", lambda **_kwargs: {})

    result = cli._doctor(state_root=tmp_path)

    assert result["runtime_provider"] == status
    assert result["runtime_provider"]["state"] == "mixed"
    assert result["ae_import"] is False
    assert result["ok"] is False
