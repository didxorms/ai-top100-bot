import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from bot.conversation_state import ConversationStore, session_key_from_event


class ConversationStateTests(unittest.TestCase):
    def test_channel_users_are_separate_even_with_a_group_field(self):
        first = session_key_from_event({"d": {
            "isChannelChatroom": True, "botUserKey": "user-a", "botGroupKey": "same-channel",
        }})
        second = session_key_from_event({"d": {
            "isChannelChatroom": True, "botUserKey": "user-b", "botGroupKey": "same-channel",
        }})
        self.assertIsNotNone(first)
        self.assertNotEqual(first, second)

    def test_group_participants_share_context_but_rooms_do_not(self):
        first = session_key_from_event({"d": {"botGroupKey": "room-a", "botUserKey": "user-a"}})
        second = session_key_from_event({"d": {"botGroupKey": "room-a", "botUserKey": "user-b"}})
        other = session_key_from_event({"d": {"botGroupKey": "room-b", "botUserKey": "user-a"}})
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, session_key_from_event({"d": {
            "isChannelChatroom": True, "botUserKey": "room-a",
        }}))

    def test_missing_identity_does_not_create_a_global_conversation(self):
        for event in ({}, {"d": None}, {"d": {"content": "hello"}}, {"d": {"isChannelChatroom": True}}):
            with self.subTest(event=event):
                self.assertIsNone(session_key_from_event(event))

    def test_store_survives_reopen_and_resets_only_one_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = ConversationStore(path)
            self.assertIsNone(store.get("one"))
            store.set("one", "resp_1")
            store.set("two", "resp_2")
            reopened = ConversationStore(path)
            self.assertEqual(reopened.get("one"), "resp_1")
            reopened.set("one", "resp_updated")
            self.assertEqual(store.get("one"), "resp_updated")
            reopened.reset("one")
            self.assertIsNone(store.get("one"))
            self.assertEqual(store.get("two"), "resp_2")

    def test_callback_claim_is_atomic_persistent_and_does_not_store_raw_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = ConversationStore(path)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(store.claim_callback, ["private-callback-token"] * 4))
            self.assertEqual(sum(results), 1)
            self.assertFalse(ConversationStore(path).claim_callback("private-callback-token"))
            self.assertNotIn(b"private-callback-token", path.read_bytes())

    def test_expired_callback_receipts_are_cleaned_up(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationStore(Path(directory) / "state.sqlite3")
            with patch("bot.conversation_state.time.time", return_value=1000):
                self.assertTrue(store.claim_callback("one"))
                self.assertFalse(store.claim_callback("one"))
            with patch("bot.conversation_state.time.time", return_value=1601):
                self.assertTrue(store.claim_callback("two"))
            connection = store._connect()
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM callback_receipts").fetchone()[0], 1)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
