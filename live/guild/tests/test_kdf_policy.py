"""KDF construction & configuration safety (refinement 2026-07-10)."""
import base64, importlib, os
import app.credentials as creds


def _reload(env):
    for k in ("GUILD_KDF_ITERS", "GUILD_ALLOW_WEAK_KDF"):
        os.environ.pop(k, None)
    os.environ.update(env)
    importlib.reload(creds)
    return creds


def teardown_module(_):
    _reload({})


def test_key_id_entropy_is_128_bits():
    assert creds.KEY_ID_LEN == 32                          # 32 hex = 128 bits
    a, b = "sk_" + "a" * 48, "sk_" + "b" * 48
    assert creds.key_id_of(a) != creds.key_id_of(b)        # distinct
    assert creds.key_id_of(a) == creds.key_id_of(a)        # deterministic lookup
    assert len(creds.key_id_of(a)) == 32


def test_production_iterations_cannot_be_set_below_floor_without_dev_mode():
    c = _reload({"GUILD_KDF_ITERS": "5"})                   # absurdly low, no dev flag
    h = c.hash_key("sk_x")
    iters = int(h.split("$")[1])
    assert iters >= c.MIN_PROD_ITERS                        # clamped up to floor


def test_weak_iterations_accepted_only_in_explicit_dev_mode():
    c = _reload({"GUILD_KDF_ITERS": "1000", "GUILD_ALLOW_WEAK_KDF": "1"})
    h = c.hash_key("sk_x")
    assert int(h.split("$")[1]) == 1000                    # honoured in dev/test


def test_verifier_format_and_salt_and_dklen():
    c = _reload({"GUILD_ALLOW_WEAK_KDF": "1", "GUILD_KDF_ITERS": "1000"})
    h = c.hash_key("sk_secret")
    scheme, iters, salt_b64, dk_b64 = h.split("$")
    assert scheme == "pbkdf2_sha256"
    assert len(base64.b64decode(salt_b64)) == 16           # 128-bit CSPRNG salt
    assert len(base64.b64decode(dk_b64)) == c.DK_LEN == 32 # explicit dklen
    assert c.verify_key_hash("sk_secret", h)
    assert not c.verify_key_hash("sk_other", h)            # constant-time compare


def test_malformed_verifier_fails_safely():
    c = _reload({})
    for bad in ("", "garbage", "pbkdf2_sha256$notanint$x$y",
                "pbkdf2_sha256$1000$!!$??", "pbkdf2_sha256$$$"):
        assert c.verify_key_hash("sk_x", bad) is False


def test_absurd_iteration_count_in_verifier_is_bounded():
    c = _reload({})
    # a tampered verifier claiming 10^12 iterations must be rejected outright,
    # never fed to PBKDF2 (which would hang).
    tampered = "pbkdf2_sha256$1000000000000$" + \
               base64.b64encode(b"0" * 16).decode() + "$" + \
               base64.b64encode(b"0" * 32).decode()
    assert c.verify_key_hash("sk_x", tampered) is False
