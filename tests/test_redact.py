from tierhopper import redact


def test_registered_secret_is_masked():
    redact.register("super-secret-value-123")
    assert "super-secret" not in redact.redact("token is super-secret-value-123 ok")


def test_key_value_patterns():
    out = redact.redact("MODAL_TOKEN_SECRET=as-abcdefghijklmnopqrstuv and api_key: 'abcdefgh12345678'")
    assert "abcdefghijklmnop" not in out
    assert "abcdefgh12345678" not in out
    assert "[REDACTED]" in out


def test_presigned_url_signature_masked():
    url = "https://x.r2.cloudflarestorage.com/b/k?X-Amz-Credential=AKIA123&X-Amz-Signature=deadbeef99"
    out = redact.redact(url)
    assert "deadbeef99" not in out and "AKIA123" not in out


def test_plain_text_untouched():
    text = "Moved to Kaggle because the free credit ran out"
    assert redact.redact(text) == text


def test_non_secret_fields_are_not_masked(monkeypatch):
    from tierhopper import credentials

    monkeypatch.setattr(credentials.keyring, "get_password", lambda service, account: "tierhopper-bucket-name")
    assert credentials.get_secret("r2", "bucket") == "tierhopper-bucket-name"
    assert redact.redact("[tierhopper-bucket-name] checkpoint saved") == "[tierhopper-bucket-name] checkpoint saved"
    assert credentials.get_secret("r2", "secret_access_key") == "tierhopper-bucket-name"  # a real secret field
    assert "tierhopper-bucket-name" not in redact.redact("key tierhopper-bucket-name")
    redact._known.discard("tierhopper-bucket-name")
