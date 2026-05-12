from pathlib import Path

from workerbee.trust import trust_status


def test_trust_status_includes_ca_command_guidance(tmp_path: Path) -> None:
    status = trust_status(tmp_path)

    assert status["commands"]["export"] == "workerbee ingress ca --output workerbee-ca.crt"
    assert status["commands"]["install_system"] == "workerbee trust install --target system"
    assert status["commands"]["install_nss"] == "workerbee trust install --target nss"
