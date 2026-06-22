"""No-AI tests for the secret vault + ingestion redaction.

Covers: redaction patterns; vault encrypt/decrypt roundtrip; ciphertext-at-rest (no
plaintext in DB); list never exposes plaintext; value-ref resolution; wrong-key fails;
and the end-to-end guarantee that a secret in a remembered message never lands in storage.
Run: python test_vault.py   (needs MEMNOS_SECRET_KEY in env/.env)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env(path=".env"):
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env()

import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.vault import Vault, VaultLocked
from core import redact

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    print("=== redaction ===")
    for label, sample in [
        ("openai", "my key is sk-proj-abcdefghijklmnop1234567890 ok"),
        ("aws", "AKIAIOSFODNN7EXAMPLE here"),
        ("memnos token", "token mnk_abcdefghijklmnopqrstuvwxyz012345"),
        ("password kv", "password: hunter2hunter2"),
        ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdEFGHijklMNOP"),
    ]:
        clean, n = redact.redact(sample)
        check(f"redacts {label}", n >= 1 and "REDACTED" in clean and "sk-proj-abcdef" not in clean
              and "AKIAIOSFODNN7EXAMPLE" not in clean and "hunter2hunter2" not in clean
              and "mnk_abcdefghijklmnop" not in clean)
    check("keeps normal text", redact.redact("I moved to Seattle in May")[1] == 0)
    check("redacts stripe key", "sk_live_" not in redact.redact("use sk_live_abcd1234efgh5678ijkl")[0])
    # entropy catch-all: a long high-entropy token with no named pattern
    ent_clean, ent_n = redact.redact("token=Zx9Qe7Lm2Pk4Rt6Vy8Bn1Cd3Fg5Hj0Ws aGq")
    check("entropy catch-all redacts unknown token", ent_n >= 1 and "Zx9Qe7Lm2Pk4Rt6Vy8Bn1Cd3Fg5Hj0Ws" not in ent_clean)
    check("entropy guard keeps long prose", redact.redact(
        "the quick brown fox jumps over the lazy dog again and again today")[1] == 0)

    print("=== vault ===")
    if not Vault.available():
        print("  SKIP — MEMNOS_SECRET_KEY not set"); sys.exit(0)
    Vault.set(conn, "test_secret", "s3cr3t-value-xyz", "unit test")
    check("get roundtrip", Vault.get(conn, "test_secret") == "s3cr3t-value-xyz")
    # ciphertext at rest, no plaintext
    with conn.cursor() as c:
        c.execute("SELECT ciphertext FROM memnos_control.secrets WHERE name='test_secret'")
        ct = bytes(c.fetchone()["ciphertext"])
    check("plaintext NOT in stored ciphertext", b"s3cr3t-value-xyz" not in ct)
    check("list returns no plaintext", all("value" not in s and "ciphertext" not in s
                                           for s in Vault.list(conn)))
    check("value-ref resolves", Vault.resolve(conn, "secret://test_secret") == "s3cr3t-value-xyz")
    check("non-ref passes through", Vault.resolve(conn, "plain") == "plain")
    # wrong key cannot decrypt
    saved = os.environ["MEMNOS_SECRET_KEY"]
    os.environ["MEMNOS_SECRET_KEY"] = Vault.keygen()
    try:
        Vault.get(conn, "test_secret"); check("wrong key fails to decrypt", False)
    except Exception:
        check("wrong key fails to decrypt", True)
    os.environ["MEMNOS_SECRET_KEY"] = saved

    print("=== key rotation ===")
    Vault.set(conn, "rot_secret", "rotate-me-please")
    new_key = Vault.keygen()
    n_rot, skipped = Vault.rotate_key(conn, saved, new_key)
    check("rotate re-encrypted >=1 secret", n_rot >= 1)
    os.environ["MEMNOS_SECRET_KEY"] = new_key
    check("decrypts under NEW key after rotate", Vault.get(conn, "rot_secret") == "rotate-me-please")
    os.environ["MEMNOS_SECRET_KEY"] = saved
    try:
        Vault.get(conn, "rot_secret"); check("OLD key fails after rotate", False)
    except Exception:
        check("OLD key fails after rotate", True)
    os.environ["MEMNOS_SECRET_KEY"] = new_key   # delete needs a working key path; name-AAD only
    Vault.delete(conn, "rot_secret")
    os.environ["MEMNOS_SECRET_KEY"] = saved

    print("=== end-to-end: secret in remembered text never stored ===")
    from core import BrainStore
    from core.service import MemnosMemory
    def fake_embed(t):
        import hashlib
        h = hashlib.sha256(t.encode()).digest()
        return [b / 255.0 for b in (h * 48)][:1536]
    st = BrainStore(conn=conn)
    schema = st.create_schema("vaulttest", dim=1536)
    mem = MemnosMemory(st, fake_embed, dim=1536, llm=None)   # llm=None -> no extraction, redaction still runs
    mem.schema = schema
    ns = "test:redact"
    mem.remember(ns, "here is my openai key sk-proj-SECRETSECRETSECRET1234567890 keep it safe")
    with conn.cursor() as c:
        c.execute(f"SELECT string_agg(text,' ') b FROM {schema}.raw_turns WHERE namespace=%s", (ns,))
        stored = c.fetchone()["b"] or ""
    check("remembered turn has NO raw secret", "sk-proj-SECRETSECRET" not in stored and "REDACTED" in stored)

    # cleanup
    Vault.delete(conn, "test_secret")
    st.drop_schema("vaulttest")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
