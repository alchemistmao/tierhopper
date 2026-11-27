"""Tests never touch the real system keychain or the user's TierHopper folder.

Without this, a test that saves and deletes a secret (e.g. a Kaggle token) deleted the real one from the
keychain of whoever ran the suite.
"""

import keyring
import pytest
from keyring.errors import PasswordDeleteError


@pytest.fixture(autouse=True)
def _no_real_secrets(tmp_path_factory, monkeypatch):
    vault: dict[tuple[str, str], str] = {}

    def delete(service, account):
        if (service, account) not in vault:
            raise PasswordDeleteError("not found")
        del vault[(service, account)]

    monkeypatch.setattr(keyring, "get_password", lambda service, account: vault.get((service, account)))
    monkeypatch.setattr(keyring, "set_password", lambda service, account, value: vault.__setitem__(
        (service, account), value))
    monkeypatch.setattr(keyring, "delete_password", delete)
    monkeypatch.setenv("TIERHOPPER_HOME", str(tmp_path_factory.mktemp("tierhopper-home")))
    for name in ("KAGGLE_API_TOKEN", "LIGHTNING_API_KEY", "LIGHTNING_USER_ID"):
        monkeypatch.delenv(name, raising=False)
