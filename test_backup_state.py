import tempfile
import unittest
from pathlib import Path

from backup_state import backup_database
from conversation_state import ConversationStore


class BackupTests(unittest.TestCase):
    def test_backup_preserves_memory_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "state.sqlite3"
            destination = Path(directory) / "backup.sqlite3"
            store = ConversationStore(source)
            store.set("session", "resp_keep")
            backup_database(source, destination)
            self.assertEqual(ConversationStore(destination).get("session"), "resp_keep")
            with self.assertRaises(FileExistsError):
                backup_database(source, destination)
            self.assertEqual(store.get("session"), "resp_keep")

    def test_missing_database_is_not_created_by_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "missing.sqlite3"
            destination = Path(directory) / "backup.sqlite3"
            with self.assertRaises(FileNotFoundError):
                backup_database(source, destination)
            self.assertFalse(source.exists())
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
