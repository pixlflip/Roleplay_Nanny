"""Offline storage tests. Run with python -m unittest discover -s tests."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from cryptography.fernet import Fernet

from nanny.store import DEFAULT_MODEL, SCHEMA_VERSION, Store, _MIGRATIONS


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "data" / "chronicle.db"
        self.key = Fernet.generate_key()
        self.store = Store(self.path, self.key)
        self.session = self.store.create_session(101, 201, 301, "Moonfall", "The guide", "A ruined city.")
        self.sid = self.session["id"]

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def reopen(self, key=None):
        self.store.close()
        self.store = Store(self.path, self.key if key is None else key)

    def test_schema_and_private_file(self):
        with sqlite3.connect(self.path) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_existing_permissions_are_preserved_and_insecure_access_warned(self):
        self.store.close()
        self.path.chmod(0o640)
        with self.assertLogs("nanny.store", level="WARNING") as logs:
            self.reopen()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertIn("other local users", logs.output[0])

    def test_settings_defaults_and_field_preservation(self):
        self.assertIsNone(self.store.get_settings(101))
        settings = self.store.set_settings(101, api_key="sk-or-test-one")
        self.assertEqual(settings["api_key"], "sk-or-test-one")
        self.assertEqual(settings["model"], DEFAULT_MODEL)
        self.assertEqual(settings["image_model"], "")
        self.store.set_settings(101, model="anthropic/claude-sonnet", image_model="provider/image")
        settings = self.store.set_settings(101, image_model="")
        self.assertEqual(settings["model"], "anthropic/claude-sonnet")
        self.assertEqual(settings["api_key"], "sk-or-test-one")
        self.assertEqual(settings["image_model"], "")
        self.assertIsNone(self.store.get_settings(102))
        self.assertNotIn("api_key_encrypted", settings)

    def test_key_encrypted_at_rest_and_survives_restart(self):
        secret = "sk-or-test-not-stored-in-plaintext"
        self.store.set_settings(101, api_key=secret)
        with sqlite3.connect(self.path) as database:
            token = database.execute("SELECT api_key_encrypted FROM user_settings WHERE user_id=101").fetchone()[0]
        self.assertNotEqual(token, secret.encode())
        self.assertEqual(Fernet(self.key).decrypt(token).decode(), secret)
        self.reopen()
        self.assertEqual(self.store.get_settings(101)["api_key"], secret)
        self.store.close()
        self.assertNotIn(secret.encode(), self.path.read_bytes())

    def test_wrong_key_fails_closed_and_can_be_replaced(self):
        self.store.set_settings(101, api_key="sk-or-original")
        self.reopen(Fernet.generate_key())
        with self.assertRaisesRegex(ValueError, "cannot be decrypted"):
            self.store.get_settings(101)
        self.store.set_settings(101, api_key="sk-or-replacement")
        self.assertEqual(self.store.get_settings(101)["api_key"], "sk-or-replacement")

    def test_forget_key_keeps_models_and_campaign(self):
        self.store.set_settings(101, api_key="sk-or-test", model="provider/model")
        self.store.set_settings(102, api_key="sk-or-other")
        self.store.forget_key(101)
        self.store.forget_key(999)
        settings = self.store.get_settings(101)
        self.assertIsNone(settings["api_key"])
        self.assertEqual(settings["model"], "provider/model")
        self.assertEqual(self.store.get_settings(102)["api_key"], "sk-or-other")
        self.assertEqual(self.store.get_session(self.sid, 101)["title"], "Moonfall")

    def test_invalid_fernet_key_rejected_without_database_creation(self):
        path = Path(self.directory.name) / "invalid.db"
        for key in ("bad-key", "💚", None):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Store(path, key)
        self.assertFalse(path.exists())

    def test_session_shapes_and_filters(self):
        self.assertIsInstance(self.sid, int)
        self.assertIs(self.session["archived"], False)
        self.assertEqual(self.store.get_channel_session(301, 101)["id"], self.sid)
        self.assertIsNone(self.store.get_channel_session(301, 102))
        self.store.create_session(101, 202, 302, "Other guild", "Guide", "Start")
        self.store.create_session(102, 201, 303, "Other owner", "Guide", "Start")
        self.assertEqual([s["id"] for s in self.store.list_sessions(101, 201)], [self.sid])
        self.assertEqual(self.store.list_sessions(103, 201), [])

    def test_archive_retains_data_and_prevents_writes(self):
        self.store.add_entry(self.sid, 101, "note", "Clue", "The door is blue.")
        self.store.append_turn(self.sid, 101, "Look", "A door", "before-archive")
        self.store.set_archived(self.sid, 101, True)
        self.assertIsNone(self.store.get_channel_session(301, 101))
        self.assertTrue(self.store.list_sessions(101, 201)[0]["archived"])
        self.assertEqual(len(self.store.history(self.sid, 101)), 2)
        self.assertEqual(len(self.store.list_entries(self.sid, 101)), 1)
        with self.assertRaisesRegex(ValueError, "archived"):
            self.store.add_entry(self.sid, 101, "note", "New clue", "Locked")
        with self.assertRaisesRegex(ValueError, "archived"):
            self.store.append_turn(self.sid, 101, "Open", "No", "after-archive")
        self.assertFalse(self.store.append_turn(self.sid, 101, "Look", "A door", "before-archive"))
        self.store.set_archived(self.sid, 101, False)
        self.assertTrue(self.store.append_turn(self.sid, 101, "Open", "Yes", "after-resume"))

    def test_active_channel_uniqueness_and_resume_conflict(self):
        with self.assertRaisesRegex(ValueError, "active session"):
            self.store.create_session(102, 201, 301, "Duplicate", "Guide", "Start")
        self.store.set_archived(self.sid, 101, True)
        second = self.store.create_session(101, 201, 301, "New campaign", "Guide", "Start")
        with self.assertRaisesRegex(ValueError, "another active"):
            self.store.set_archived(self.sid, 101, False)
        self.assertTrue(self.store.get_session(self.sid, 101)["archived"])
        self.store.set_archived(second["id"], 101, True)
        self.store.set_archived(self.sid, 101, False)

    def test_entries_upsert_and_filter(self):
        entry = self.store.add_entry(self.sid, 101, "item", "Silver Key", "Count: 1")
        updated = self.store.add_entry(self.sid, 101, "item", "silver key", "Count: 2")
        self.assertEqual(entry["id"], updated["id"])
        self.assertEqual(entry["created_at"], updated["created_at"])
        self.assertEqual(updated["content"], "Count: 2")
        self.store.add_entry(self.sid, 101, "fact", "silver key", "Made by dwarves")
        self.assertEqual(len(self.store.list_entries(self.sid, 101)), 2)
        self.assertEqual(len(self.store.list_entries(self.sid, 101, "item")), 1)
        self.store.delete_entry(self.sid, 101, entry["id"])
        self.assertEqual(self.store.list_entries(self.sid, 101, "item"), [])
        with self.assertRaisesRegex(ValueError, "Entry not found"):
            self.store.delete_entry(self.sid, 101, entry["id"])

    def test_entry_edit_keeps_kind_and_prevents_name_collision(self):
        entry = self.store.add_entry(self.sid, 101, "quest", "Search", "Find the tower")
        edited = self.store.update_entry(self.sid, 101, entry["id"], "Search done", "Tower found")
        self.assertEqual(edited["id"], entry["id"])
        self.assertEqual(edited["kind"], "quest")
        self.assertEqual(edited["name"], "Search done")
        other = self.store.add_entry(self.sid, 101, "quest", "Return", "Walk home")
        with self.assertRaisesRegex(ValueError, "already has that name"):
            self.store.update_entry(self.sid, 101, other["id"], "search DONE", "Collision")
        self.assertEqual(self.store.list_entries(self.sid, 101)[1]["content"], "Walk home")

    def test_entry_id_cannot_target_another_session(self):
        entry = self.store.add_entry(self.sid, 101, "note", "Secret", "Keep")
        other = self.store.create_session(101, 201, 302, "Second", "Guide", "Start")
        with self.assertRaises(ValueError):
            self.store.delete_entry(other["id"], 101, entry["id"])
        with self.assertRaises(ValueError):
            self.store.update_entry(other["id"], 101, entry["id"], "New", "Wrong")
        self.assertEqual(self.store.list_entries(self.sid, 101)[0]["content"], "Keep")

    def test_all_owner_scoped_operations_reject_other_owner(self):
        entry = self.store.add_entry(self.sid, 101, "note", "Secret", "Keep")
        self.store.append_turn(self.sid, 101, "Hello", "There", "original")
        operations = [
            lambda owner: self.store.get_session(self.sid, owner),
            lambda owner: self.store.set_archived(self.sid, owner, True),
            lambda owner: self.store.add_entry(self.sid, owner, "fact", "Secret", "Changed"),
            lambda owner: self.store.delete_entry(self.sid, owner, entry["id"]),
            lambda owner: self.store.update_entry(self.sid, owner, entry["id"], "New", "Changed"),
            lambda owner: self.store.list_entries(self.sid, owner),
            lambda owner: self.store.history(self.sid, owner),
            lambda owner: self.store.append_turn(self.sid, owner, "Hello", "There", "original"),
            lambda owner: self.store.get_turn(self.sid, owner, "original"),
            lambda owner: self.store.claim_turn(self.sid, owner, "new"),
            lambda owner: self.store.fail_turn(self.sid, owner, "original"),
            lambda owner: self.store.edit_last_reply(self.sid, owner, "Changed"),
            lambda owner: self.store.export_session(self.sid, owner),
        ]
        for index, operation in enumerate(operations):
            with self.subTest(operation=index), self.assertRaises(ValueError):
                operation(102)
            # get_session(None) is explicitly allowed for internal lookup only.
            if index:
                with self.subTest(operation=index, owner=None), self.assertRaises(ValueError):
                    operation(None)
        self.assertEqual(self.store.history(self.sid, 101)[-1]["content"], "There")

    def test_missing_session_is_not_distinguishable_from_wrong_owner(self):
        errors = []
        for sid, owner in ((self.sid, 102), (99999, 101)):
            with self.assertRaises(ValueError) as caught:
                self.store.get_session(sid, owner)
            errors.append(str(caught.exception))
        self.assertEqual(*errors)

    def test_history_is_recent_chronological_and_persistent(self):
        for number in range(4):
            self.store.append_turn(self.sid, 101, f"User {number}", f"Guide {number}", str(number))
        self.reopen()
        self.assertEqual(self.store.history(self.sid, 101, 3), [
            {"role": "assistant", "content": "Guide 2"},
            {"role": "user", "content": "User 3"},
            {"role": "assistant", "content": "Guide 3"},
        ])

    def test_append_turn_idempotency_and_edit(self):
        with self.assertRaisesRegex(ValueError, "no assistant"):
            self.store.edit_last_reply(self.sid, 101, "Replacement")
        self.assertTrue(self.store.append_turn(self.sid, 101, "Hello", "There", 1234))
        self.assertFalse(self.store.append_turn(self.sid, 101, "Hello", "Different", "1234"))
        self.store.edit_last_reply(self.sid, 101, "Replacement")
        self.assertEqual(self.store.get_turn(self.sid, 101, 1234), "Replacement")
        self.assertEqual(len(self.store.history(self.sid, 101)), 2)
        self.reopen()
        self.assertFalse(self.store.append_turn(self.sid, 101, "Hello", "There", 1234))

    def test_atomic_rollback_if_assistant_write_fails(self):
        # Fault injection exercises the actual transaction, not a mocked method.
        self.store._connection.execute(
            """CREATE TRIGGER reject_assistant BEFORE INSERT ON messages
               WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_turn(self.sid, 101, "Hello", "There", "atomic")
        self.assertEqual(self.store.history(self.sid, 101), [])
        self.assertEqual(self.store._connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)
        self.store._connection.execute("DROP TRIGGER reject_assistant")
        self.assertTrue(self.store.append_turn(self.sid, 101, "Hello", "There", "atomic"))

    def test_failed_commit_rolls_back_and_connection_recovers(self):
        self.store._connection.execute(
            """CREATE TABLE deferred_failure (
                   parent_id INTEGER REFERENCES sessions(id) DEFERRABLE INITIALLY DEFERRED
               )"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._transaction():
                self.store._connection.execute("INSERT INTO deferred_failure VALUES(999999)")
        self.assertFalse(self.store._connection.in_transaction)
        self.assertEqual(self.store._connection.execute("SELECT COUNT(*) FROM deferred_failure").fetchone()[0], 0)
        self.assertTrue(self.store.append_turn(self.sid, 101, "Hello", "There", "after-failure"))

    def test_failed_pair_write_preserves_durable_pending_claim(self):
        self.store.claim_turn(self.sid, 101, "claimed")
        self.store._connection.execute(
            """CREATE TRIGGER reject_assistant BEFORE INSERT ON messages
               WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_turn(self.sid, 101, "Hello", "There", "claimed")
        self.assertEqual(self.store.history(self.sid, 101), [])
        self.assertEqual(self.store._connection.execute("SELECT status FROM turns").fetchone()[0], "pending")
        self.assertFalse(self.store.claim_turn(self.sid, 101, "claimed"))

    def test_claims_survive_restart_and_complete_atomically(self):
        self.assertTrue(self.store.claim_turn(self.sid, 101, "paid-call"))
        self.assertFalse(self.store.claim_turn(self.sid, 101, "paid-call"))
        self.assertIsNone(self.store.get_turn(self.sid, 101, "paid-call"))
        self.reopen()
        self.assertFalse(self.store.claim_turn(self.sid, 101, "paid-call"))
        self.assertTrue(self.store.append_turn(self.sid, 101, "Hello", "There", "paid-call"))
        self.assertFalse(self.store.claim_turn(self.sid, 101, "paid-call"))
        self.assertEqual(self.store.get_turn(self.sid, 101, "paid-call"), "There")
        self.assertFalse(self.store.append_turn(self.sid, 101, "Hello", "There", "paid-call"))

    def test_failed_claims_are_retained(self):
        self.assertTrue(self.store.claim_turn(self.sid, 101, "uncertain"))
        self.store.fail_turn(self.sid, 101, "uncertain")
        self.reopen()
        self.assertFalse(self.store.claim_turn(self.sid, 101, "uncertain"))
        with self.assertRaisesRegex(ValueError, "already failed"):
            self.store.append_turn(self.sid, 101, "Hello", "There", "uncertain")
        self.assertIsNone(self.store.get_turn(self.sid, 101, "uncertain"))
        self.assertTrue(self.store.claim_turn(self.sid, 101, "new-message"))

    def test_failing_a_complete_turn_is_harmless(self):
        self.store.append_turn(self.sid, 101, "Hello", "There", "done")
        self.store.fail_turn(self.sid, 101, "done")
        self.assertEqual(self.store.get_turn(self.sid, 101, "done"), "There")

    def test_threads_and_connections_cannot_duplicate_turns(self):
        second_store = Store(self.path, self.key)
        self.addCleanup(second_store.close)

        def add(number):
            store = self.store if number % 2 else second_store
            return store.append_turn(self.sid, 101, "Hello", "There", "concurrent")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(add, range(24)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.store.history(self.sid, 101)), 2)

    def test_export_is_complete_and_contains_no_settings(self):
        self.store.set_settings(101, api_key="sk-or-secret-for-export-test")
        self.store.add_entry(self.sid, 101, "fact", "Home", "A quiet valley")
        for number in range(60):
            self.store.append_turn(self.sid, 101, f"u{number}", f"a{number}", str(number))
        exported = self.store.export_session(self.sid, 101)
        self.assertEqual(set(exported), {"format_version", "session", "entries", "messages"})
        self.assertEqual(len(exported["messages"]), 120)
        self.assertEqual(exported["entries"][0]["name"], "Home")
        serialized = json.dumps(exported)
        self.assertNotIn("sk-or-secret", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("api_key_encrypted", serialized)

    def test_validation_rejects_bad_values(self):
        invalid_operations = [
            lambda: self.store.set_settings(0, api_key="key"),
            lambda: self.store.set_settings(True, api_key="key"),
            lambda: self.store.set_settings(101, api_key="bad key"),
            lambda: self.store.set_settings(101, api_key="bad\x01key"),
            lambda: self.store.set_settings(101, api_key="bad🔑key"),
            lambda: self.store.set_settings(101, api_key="x" * 513),
            lambda: self.store.set_settings(101, model=""),
            lambda: self.store.set_settings(101, model="provider model"),
            lambda: self.store.set_settings(101, image_model="provider\nmodel"),
            lambda: self.store.create_session(101, 201, 302, "t" * 101, "Guide", "Start"),
            lambda: self.store.create_session(101, 201, 302, "Title", "", "Start"),
            lambda: self.store.create_session(101, 201, 302, "Title", "Guide", "\x00"),
            lambda: self.store.create_session(101, 201, 302, "Title", "Guide", "\ud800"),
            lambda: self.store.add_entry(self.sid, 101, "inventory", "Key", "One"),
            lambda: self.store.add_entry(self.sid, 101, "item", " ", "One"),
            lambda: self.store.add_entry(self.sid, 101, "item", "Key", "x" * 8_001),
            lambda: self.store.list_entries(self.sid, 101, "system"),
            lambda: self.store.append_turn(self.sid, 101, "x" * 32_001, "Hi", "request"),
            lambda: self.store.claim_turn(self.sid, 101, ""),
            lambda: self.store.set_archived(self.sid, 101, "yes"),
        ]
        for index, operation in enumerate(invalid_operations):
            with self.subTest(operation=index), self.assertRaises(ValueError):
                operation()
        for limit in (0, -1, 101, 1.5, True, "30"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.history(self.sid, 101, limit)

    def test_parameterized_queries_preserve_literal_text(self):
        text = "Robert'); DROP TABLE sessions;--"
        entry = self.store.add_entry(self.sid, 101, "note", text, text)
        self.assertEqual(entry["name"], text)
        self.assertEqual(self.store.get_session(self.sid, 101)["title"], "Moonfall")

    def test_close_is_idempotent(self):
        self.store.close()
        self.store.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.store.get_settings(101)

    def test_empty_database_path_rejected(self):
        with self.assertRaisesRegex(ValueError, "nonempty"):
            Store("", self.key)

    def test_context_manager_and_in_memory_storage(self):
        with Store(":memory:", self.key.decode()) as temporary:
            temporary.set_settings(101, model="provider/model")
            self.assertEqual(temporary.get_settings(101)["model"], "provider/model")
        with self.assertRaises(RuntimeError):
            temporary.get_settings(101)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "chronicle.db"
        self.key = Fernet.generate_key()

    def tearDown(self):
        self.directory.cleanup()

    def test_upgrade_keeps_existing_campaign_and_key(self):
        with sqlite3.connect(self.path) as database:
            for statement in _MIGRATIONS[1]:
                database.execute(statement)
            database.execute("PRAGMA user_version = 1")
            database.execute(
                "INSERT INTO user_settings VALUES(101, ?, ?, '', 'old-time')",
                (Fernet(self.key).encrypt(b"sk-or-preserved"), DEFAULT_MODEL),
            )
            database.execute(
                "INSERT INTO sessions VALUES(1, 101, 201, 301, 'Old title', 'Guide', 'Start', 0, 'then', 'then')"
            )
            database.execute("INSERT INTO turns VALUES(1, 1, 'old-request', 'then')")
            database.execute("INSERT INTO messages VALUES(1, 1, 1, 'user', 'Hello', 'then')")
            database.execute("INSERT INTO messages VALUES(2, 1, 1, 'assistant', 'There', 'then')")
        if os.name == "posix":
            self.path.chmod(0o600)
        with Store(self.path, self.key) as store:
            self.assertEqual(store.get_settings(101)["api_key"], "sk-or-preserved")
            self.assertEqual(store.get_session(1, 101)["title"], "Old title")
            self.assertEqual(store.get_turn(1, 101, "old-request"), "There")
            self.assertFalse(store.claim_turn(1, 101, "old-request"))
            self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_refuses_future_schema(self):
        with sqlite3.connect(self.path) as database:
            database.execute("PRAGMA user_version = 999")
        with self.assertRaisesRegex(RuntimeError, "newer"):
            Store(self.path, self.key)
        with sqlite3.connect(self.path) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_does_not_adopt_or_destroy_legacy_database(self):
        with sqlite3.connect(self.path) as database:
            database.execute("CREATE TABLE guilds(id INTEGER PRIMARY KEY, data TEXT)")
            database.execute("INSERT INTO guilds VALUES(1, 'legacy-data')")
        with self.assertRaisesRegex(RuntimeError, "preserve the legacy"):
            Store(self.path, self.key)
        with sqlite3.connect(self.path) as database:
            self.assertEqual(database.execute("SELECT data FROM guilds").fetchone()[0], "legacy-data")
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 0)
            tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"guilds"})


if __name__ == "__main__":
    unittest.main()
