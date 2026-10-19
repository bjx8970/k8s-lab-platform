import json
import logging
import os
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_real_path_exists = os.path.exists


def _test_path_exists(path):
    if os.path.basename(path) == ".db_config.json":
        return False
    return _real_path_exists(path)


with patch("os.path.exists", side_effect=_test_path_exists):
    from modules import db
from modules.audit import (
    REDACTED,
    SecretTextSanitizer,
    _AUDIT_LOGGER,
    safe_error_message,
    sanitize,
    sanitize_text,
    security_audit,
)
from modules.credential_store import (
    CREDENTIAL_ERROR_MESSAGE, CredentialError, decrypt_secret, encrypt_secret,
)


class SecretBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_engine = db.engine
        cls.original_session_local = db.SessionLocal
        cls.test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        db.engine = cls.test_engine
        db.SessionLocal = sessionmaker(bind=cls.test_engine)
        db.Base.metadata.create_all(cls.test_engine)

    @classmethod
    def tearDownClass(cls):
        db.Base.metadata.drop_all(cls.test_engine)
        cls.test_engine.dispose()
        db.engine = cls.original_engine
        db.SessionLocal = cls.original_session_local

    def setUp(self):
        db.Base.metadata.drop_all(self.test_engine)
        db.Base.metadata.create_all(self.test_engine)
        self.key = Fernet.generate_key().decode("ascii")
        self.key_env = patch.dict(os.environ, {"K8S_LAB_CREDENTIAL_KEY": self.key})
        self.key_env.start()

    def tearDown(self):
        self.key_env.stop()

    def test_credential_store_fails_closed_without_leaking_secret(self):
        secret = "password-do-not-return"
        ciphertext = encrypt_secret(secret)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CredentialError) as ctx:
                encrypt_secret(secret)
            self.assertNotIn(secret, str(ctx.exception))
            with self.assertRaises(CredentialError):
                decrypt_secret(ciphertext)
        with patch.dict(os.environ, {"K8S_LAB_CREDENTIAL_KEY": self.key + "bad"}):
            with self.assertRaises(CredentialError):
                decrypt_secret(ciphertext)
        wrong_key = Fernet.generate_key().decode("ascii")
        with patch.dict(os.environ, {"K8S_LAB_CREDENTIAL_KEY": wrong_key}):
            for operation in (encrypt_secret, decrypt_secret):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaises(CredentialError) as ctx:
                        operation(ciphertext)
                    self.assertEqual(str(ctx.exception), CREDENTIAL_ERROR_MESSAGE)
        self.assertEqual(encrypt_secret(ciphertext), ciphertext)
        self.assertEqual(decrypt_secret(ciphertext), secret)
        for value in (None, ""):
            self.assertEqual(encrypt_secret(value), value)
            self.assertEqual(decrypt_secret(value), value)
        for operation in (encrypt_secret, decrypt_secret):
            with self.assertRaises(CredentialError):
                operation("enc:v1:garbage")
        with self.assertRaises(CredentialError):
            decrypt_secret(secret)
        with patch("modules.credential_store.Fernet", side_effect=ValueError(secret)):
            try:
                encrypt_secret(secret)
            except CredentialError:
                rendered = traceback.format_exc()
        self.assertNotIn(secret, rendered)

    def test_pve_roundtrip_encrypts_and_redacted_update_preserves_credentials(self):
        token = "token-secret"
        password = "openwrt-secret"
        sid = db.create_pve_server({
            "name": "pve-test",
            "host": "pve.invalid",
            "user": "root@pam",
            "token_name": "lab-token",
            "token_value": token,
            "node": "node-a",
            "ow_password": password,
        })
        with db.session_scope() as session:
            row = session.get(db.PVEServer, sid)
            self.assertNotIn(token, row.token_value)
            self.assertNotIn(password, row.ow_password)
            self.assertTrue(row.token_value.startswith("enc:v1:"))
            stored_before = (row.token_value, row.ow_password)
        public = db.list_pve_servers()[0]
        self.assertEqual(public["token_name"], "lab-token")
        self.assertEqual(public["token_value"], REDACTED)
        self.assertEqual(public["ow_password"], REDACTED)
        public["name"] = "pve-renamed"
        db.update_pve_server(sid, public)
        db.update_pve_server(sid, {
            "name": "pve-renamed",
            "token_name": REDACTED,
            "token_value": REDACTED,
            "ow_password": "",
        })
        self.assertEqual(db.get_pve_server(sid)["token_name"], "lab-token")
        self.assertEqual(db.get_pve_server(sid)["token_value"], token)
        self.assertEqual(db.get_pve_server(sid)["ow_password"], password)
        for marker in (None, "", "****", REDACTED):
            with self.subTest(marker=marker):
                db.update_pve_server(sid, {
                    "token_name": marker, "token_value": marker, "ow_password": marker,
                })
                with db.session_scope() as session:
                    row = session.get(db.PVEServer, sid)
                    self.assertEqual(stored_before, (row.token_value, row.ow_password))
                    self.assertEqual(row.token_name, "lab-token")
        db.update_pve_server(sid, {"name": "only-name"})
        with db.session_scope() as session:
            row = session.get(db.PVEServer, sid)
            self.assertEqual(stored_before, (row.token_value, row.ow_password))
        with patch.dict(os.environ, {"K8S_LAB_CREDENTIAL_KEY": ""}):
            self.assertEqual(db.list_pve_servers()[0]["token_value"], REDACTED)
            with self.assertRaises(CredentialError):
                db.get_pve_server(sid)
        with self.assertRaises(ValueError):
            db.create_pve_server({
                "name": "bad",
                "host": "pve.invalid",
                "user": "root@pam",
                "token_name": "lab-token",
                "token_value": REDACTED,
            })

    def test_config_and_cluster_secrets_are_encrypted(self):
        token = "legacy-token"
        password = "legacy-password"
        private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----"
        db.set_config("pve", {
            "host": "pve.invalid",
            "user": "root@pam",
            "token_name": "lab-token",
            "token_value": token,
        })
        db.set_config("openwrt", {"host": "ow.invalid", "password": password})
        db.save_cluster("k8s_1", {"ssh_private_key": private_key, "vms": {}})
        with db.session_scope() as session:
            self.assertNotIn(token, session.get(db.Config, "pve").value)
            self.assertNotIn(password, session.get(db.Config, "openwrt").value)
            self.assertNotIn(private_key, session.get(db.Cluster, 1).ssh_private_key)
        self.assertEqual(db.get_config("pve")["token_value"], token)
        self.assertEqual(db.get_config("openwrt")["password"], password)
        self.assertEqual(db.load_cluster("k8s_1")["ssh_private_key"], private_key)

    def test_legacy_config_updates_preserve_and_copy_encrypted_secrets(self):
        db.set_config("pve", {
            "host": "pve.invalid", "user": "root@pam", "token_name": "lab-token",
            "token_value": "legacy-token",
        })
        db.set_config("openwrt", {"host": "ow.invalid", "password": "legacy-password"})
        with db.session_scope() as session:
            old_pve = json.loads(session.get(db.Config, "pve").value)
            old_ow = json.loads(session.get(db.Config, "openwrt").value)
        for marker in (None, "", "****", REDACTED):
            db.set_config("pve", {"token_name": marker, "token_value": marker})
            db.set_config("openwrt", {"password": marker})
        db.set_config("pve", {"name": "renamed"})
        db.set_config("openwrt", {"name": "renamed"})
        with db.session_scope() as session:
            self.assertEqual(json.loads(session.get(db.Config, "pve").value)["token_value"], old_pve["token_value"])
            self.assertEqual(json.loads(session.get(db.Config, "openwrt").value)["password"], old_ow["password"])
        db.migrate_pve_config()
        db._migrate_openwrt_to_pve_servers()
        with db.session_scope() as session:
            row = session.query(db.PVEServer).one()
            self.assertEqual(row.token_value, old_pve["token_value"])
            self.assertEqual(row.ow_password, old_ow["password"])
            self.assertEqual(row.token_name, "lab-token")
            sid = row.id
            self.assertIsNone(session.get(db.Config, "pve"))
        self.assertEqual(db.get_pve_server(sid)["token_value"], "legacy-token")
        self.assertEqual(db.get_pve_server(sid)["ow_password"], "legacy-password")
        db.set_config("webssh", {"first": 1, "second": 2})
        db.set_config("webssh", {"first": 3})
        self.assertEqual(db.get_config("webssh"), {"first": 3})
        for key, field in (("pve", "token_value"), ("openwrt", "password")):
            db.delete_config(key)
            for marker in ("****", REDACTED):
                with self.assertRaises(ValueError):
                    db.set_config(key, {field: marker})

    def test_plaintext_migration_is_idempotent_and_rolls_back(self):
        token = "plaintext-token"
        password = "plaintext-password"
        with db.session_scope(commit=True) as session:
            session.add(db.PVEServer(
                name="legacy", host="pve.invalid", user="root@pam",
                token_name="lab", token_value=token, ow_password=password,
            ))
            session.add(db.Config(
                key="openwrt", value=json.dumps({"password": password}),
            ))
            session.add(db.Cluster(
                name="legacy-cluster", ssh_private_key="plaintext-key",
            ))
        db.migrate_plaintext_credentials()
        with db.session_scope() as session:
            server = session.query(db.PVEServer).one()
            first = (server.token_value, server.ow_password)
            self.assertNotIn(token, first[0])
            self.assertNotIn(password, first[1])
        db.migrate_plaintext_credentials()
        with db.session_scope() as session:
            server = session.query(db.PVEServer).one()
            self.assertEqual(first, (server.token_value, server.ow_password))

        with db.session_scope(commit=True) as session:
            session.add(db.PVEServer(
                name="legacy-rollback", host="pve.invalid", user="root@pam",
                token_name="lab", token_value="rollback-token", ow_password="rollback-password",
            ))
            session.add(db.Config(key="pve", value=json.dumps({"token_value": "rollback-config"})))
            session.add(db.Cluster(name="rollback-cluster", ssh_private_key="rollback-key"))
        original_encrypt = db.encrypt_secret
        encrypted_inputs = []

        def fail_at_last_table(value):
            encrypted_inputs.append(value)
            if value == "rollback-key":
                raise RuntimeError("deliberate isolated failure")
            return original_encrypt(value)

        with patch.object(db, "encrypt_secret", side_effect=fail_at_last_table):
            with self.assertRaises(RuntimeError):
                db.migrate_plaintext_credentials()
        self.assertIn("rollback-token", encrypted_inputs)
        self.assertIn("rollback-config", encrypted_inputs)
        with db.session_scope() as session:
            rollback = session.query(db.PVEServer).filter_by(name="legacy-rollback").one()
            self.assertEqual(rollback.token_value, "rollback-token")
            self.assertEqual(rollback.ow_password, "rollback-password")
            self.assertEqual(json.loads(session.get(db.Config, "pve").value)["token_value"], "rollback-config")
            self.assertEqual(session.query(db.Cluster).filter_by(name="rollback-cluster").one().ssh_private_key, "rollback-key")

    def test_migration_authenticates_existing_ciphertext_and_rolls_back_on_wrong_key(self):
        valid_ciphertext = encrypt_secret("old-secret")
        with db.session_scope(commit=True) as session:
            session.add(db.PVEServer(
                name="pending-plaintext", host="pve.invalid", user="root@pam",
                token_name="lab", token_value="pending-token", ow_password="pending-password",
            ))
            session.add(db.Config(key="openwrt", value=json.dumps({"password": "pending-config"})))
            session.add(db.Cluster(name="encrypted-cluster", ssh_private_key=valid_ciphertext))
        for existing_ciphertext, key in (
            (valid_ciphertext, Fernet.generate_key().decode("ascii")),
            ("enc:v1:garbage", self.key),
        ):
            with self.subTest(ciphertext_valid=existing_ciphertext == valid_ciphertext):
                with db.session_scope(commit=True) as session:
                    session.query(db.Cluster).one().ssh_private_key = existing_ciphertext
                with patch.dict(os.environ, {"K8S_LAB_CREDENTIAL_KEY": key}):
                    with self.assertRaises(CredentialError):
                        db.migrate_plaintext_credentials()
                with db.session_scope() as session:
                    row = session.query(db.PVEServer).one()
                    self.assertEqual((row.token_value, row.ow_password), ("pending-token", "pending-password"))
                    self.assertEqual(json.loads(session.get(db.Config, "openwrt").value)["password"], "pending-config")
                    self.assertEqual(session.query(db.Cluster).one().ssh_private_key, existing_ciphertext)

    def test_postgresql_migration_alters_only_the_two_secret_columns_transactionally(self):
        session = MagicMock()
        session.query.return_value.all.return_value = []
        session.query.return_value.filter.return_value.all.return_value = []
        postgres_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        with patch.object(db, "engine", postgres_engine), patch.object(db, "get_session", return_value=session):
            db.migrate_plaintext_credentials()
        self.assertEqual([str(call.args[0]) for call in session.execute.call_args_list], [
            "ALTER TABLE pve_servers ALTER COLUMN token_value TYPE TEXT",
            "ALTER TABLE pve_servers ALTER COLUMN ow_password TYPE TEXT",
        ])
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()
        session.reset_mock()
        session.execute.side_effect = RuntimeError("isolated DDL failure")
        with patch.object(db, "engine", postgres_engine), patch.object(db, "get_session", return_value=session):
            with self.assertRaises(RuntimeError):
                db.migrate_plaintext_credentials()
        session.commit.assert_not_called()
        session.rollback.assert_called_once_with()

    def test_legacy_plaintext_requires_explicit_migration(self):
        with db.session_scope(commit=True) as session:
            session.add(db.Config(key="pve", value=json.dumps({
                "host": "pve.invalid", "user": "root@pam", "token_name": "lab",
                "token_value": "plain-legacy-token",
            })))
            session.add(db.Config(key="openwrt", value=json.dumps({
                "host": "ow.invalid", "password": "plain-legacy-password",
            })))
            session.add(db.Cluster(name="plain-legacy-cluster", ssh_private_key="plain-private-key"))
        for action in (
            lambda: db.get_config("pve"),
            lambda: db.get_config("openwrt"),
            lambda: db.load_cluster("plain-legacy-cluster"),
            db.migrate_pve_config,
        ):
            with self.assertRaises(CredentialError):
                action()
        with db.session_scope() as session:
            self.assertIsNotNone(session.get(db.Config, "pve"))
            self.assertEqual(session.query(db.PVEServer).count(), 0)
        db.migrate_plaintext_credentials()
        db.migrate_pve_config()
        db._migrate_openwrt_to_pve_servers()
        sid = db.list_pve_servers()[0]["id"]
        self.assertEqual(db.get_pve_server(sid)["token_value"], "plain-legacy-token")
        self.assertEqual(db.get_pve_server(sid)["ow_password"], "plain-legacy-password")
        self.assertEqual(db.load_cluster("plain-legacy-cluster")["ssh_private_key"], "plain-private-key")


class AuditSecretTests(unittest.TestCase):
    def test_sanitize_text_handles_structured_secrets_and_uri(self):
        text = (
            '{"token_name": "visible-name", "password": "raw-password", '
            "'private_key': 'raw-private', 'url': 'https://u:raw-uri@host/x'}"
        )
        result = sanitize_text(text)
        self.assertIn("visible-name", result)
        self.assertNotIn("raw-password", result)
        self.assertNotIn("raw-private", result)
        self.assertNotIn("raw-uri", result)
        self.assertGreaterEqual(result.count(REDACTED), 3)

    def test_stream_sanitizer_suppresses_key_across_chunks_and_unclosed_body(self):
        value = (
            "before\n-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "AAAA-secret-body-not-loggable\n-----END OPENSSH PRIVATE KEY-----\n"
            "after"
        )
        sanitizer = SecretTextSanitizer()
        result = "".join(sanitizer.feed(value[i:i + 3]) for i in range(0, len(value), 3))
        result += sanitizer.flush()
        self.assertEqual(result, "before\n[REDACTED]\nafter")
        self.assertNotIn("AAAA-secret-body-not-loggable", result)

        sanitizer = SecretTextSanitizer()
        result = sanitizer.feed("ok\n-----BEGIN RSA PRIVATE KEY-----\n")
        result += sanitizer.feed("body-that-must-not-appear")
        result += sanitizer.flush()
        self.assertEqual(result, "ok\n[REDACTED]")

    def test_stream_private_key_markers_cover_all_split_positions_and_casing(self):
        for label, transform in (
            ("OPENSSH", str.upper), ("RSA", str.lower),
            ("ENCRYPTED", lambda value: "".join(ch.upper() if i % 2 else ch.lower() for i, ch in enumerate(value))),
        ):
            begin = transform(f"-----BEGIN {label} PRIVATE KEY-----")
            end = transform(f"-----END {label} PRIVATE KEY-----")
            value = f"normal-before\n{begin}\nbody-must-stay-hidden\n{end}\nnormal-after"
            for split in range(len(value) + 1):
                with self.subTest(label=label, split=split):
                    sanitizer = SecretTextSanitizer()
                    result = sanitizer.feed(value[:split]) + sanitizer.feed(value[split:]) + sanitizer.flush()
                    self.assertEqual(result, "normal-before\n[REDACTED]\nnormal-after")
            sanitizer = SecretTextSanitizer()
            result = "".join(sanitizer.feed(line) for line in value.splitlines()) + sanitizer.flush()
            self.assertEqual(result, "normal-before[REDACTED]normal-after")
        ordinary = "normal-before\n-----BEGIN PUBLIC KEY-----\npublic-data\n-----END PUBLIC KEY-----\nnormal-after"
        for split in range(len(ordinary) + 1):
            sanitizer = SecretTextSanitizer()
            self.assertEqual(sanitizer.feed(ordinary[:split]) + sanitizer.feed(ordinary[split:]) + sanitizer.flush(), ordinary)

    def test_sanitize_recurses_strings_and_token_name_is_not_secret(self):
        result = sanitize({
            "token_name": "lab-token",
            "message": "password=hidden token_name=still-visible",
            "nested": ["authorization: Bearer hidden"],
        })
        self.assertEqual(result["token_name"], "lab-token")
        self.assertNotIn("hidden", result["message"])
        self.assertNotIn("hidden", result["nested"][0])

    def test_safe_error_message_never_stringifies_exception(self):
        class Explosive:
            def __str__(self):
                raise AssertionError("must not stringify")

        message = safe_error_message(Explosive())
        self.assertEqual(message, "操作失败，请联系管理员")

    def test_default_success_audit_has_own_info_handler(self):
        class Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                self.messages.append(record.getMessage())

        capture = Capture()
        old_root = logging.getLogger().level
        _AUDIT_LOGGER.addHandler(capture)
        logging.getLogger().setLevel(logging.WARNING)
        try:
            security_audit("test.write", "success", metadata={"password": "hidden"})
        finally:
            _AUDIT_LOGGER.removeHandler(capture)
            logging.getLogger().setLevel(old_root)
        self.assertEqual(len(capture.messages), 1)
        self.assertNotIn("hidden", capture.messages[0])


if __name__ == "__main__":
    unittest.main()
