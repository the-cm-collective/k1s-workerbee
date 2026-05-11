from pathlib import Path
from stat import S_IMODE

from workerbee.secrets import file_is_sops_encrypted, seal_yaml_mapping, secret_policy_status


def test_file_is_sops_encrypted_requires_encrypted_values(tmp_path: Path) -> None:
    plain = tmp_path / "plain.yaml"
    plain.write_text("token: local\nsops: {}\n", encoding="utf-8")
    encrypted = tmp_path / "encrypted.yaml"
    encrypted.write_text("token: ENC[test]\nsops: {}\n", encoding="utf-8")

    assert file_is_sops_encrypted(plain) is False
    assert file_is_sops_encrypted(encrypted) is True
    assert file_is_sops_encrypted(tmp_path / "missing.yaml") is False


def test_seal_yaml_mapping_plaintext_requires_explicit_opt_in(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKERBEE_ALLOW_PLAINTEXT_SECRETS", "1")
    secret = tmp_path / "secret.yaml"

    assert seal_yaml_mapping(secret, {"token": "local-dev"}, project_state=tmp_path) == secret
    assert "token: local-dev" in secret.read_text(encoding="utf-8")
    assert S_IMODE(secret.stat().st_mode) == 0o600


def test_secret_policy_status_reports_missing_configured_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("WORKERBEE_ALLOW_PLAINTEXT_SECRETS", raising=False)
    monkeypatch.setenv("WORKERBEE_SOPS_AGE_KEY_FILE", str(tmp_path / "missing.txt"))

    status = secret_policy_status(tmp_path)

    assert status["mode"] == "sops"
    assert status["key_ready"] is False
    assert status["key_error"]
