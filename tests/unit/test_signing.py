import hashlib
import hmac

from app.signing import sign, verify

SECRET = "s" * 32
BODY = b'{"payment_id":"p1","tokens":10}'


def test_signature_is_hex_hmac_sha256_of_the_raw_body() -> None:
    assert sign(SECRET, BODY) == hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()


def test_valid_signature_is_accepted() -> None:
    assert verify(SECRET, BODY, sign(SECRET, BODY))


def test_signature_comparison_ignores_case_and_surrounding_spaces() -> None:
    assert verify(SECRET, BODY, f"  {sign(SECRET, BODY).upper()} ")


def test_missing_or_garbage_signature_is_rejected() -> None:
    assert not verify(SECRET, BODY, None)
    assert not verify(SECRET, BODY, "")
    assert not verify(SECRET, BODY, "not-hex-at-all")


def test_any_change_of_body_or_secret_is_rejected() -> None:
    signature = sign(SECRET, BODY)
    assert not verify(SECRET, BODY + b" ", signature)
    assert not verify("x" * 32, BODY, signature)


def test_non_ascii_signature_does_not_crash() -> None:
    assert not verify(SECRET, BODY, "подпись")
