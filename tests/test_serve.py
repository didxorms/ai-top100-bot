import os
import unittest
from unittest.mock import patch

from serve import server_options


class ServerOptionsTests(unittest.TestCase):
    def test_cloud_port_and_graceful_shutdown(self):
        with patch.dict(os.environ, {"PORT": "43210"}):
            options = server_options()
        self.assertEqual(options["host"], "0.0.0.0")
        self.assertEqual(options["port"], 43210)
        self.assertEqual(options["workers"], 1)
        self.assertEqual(options["timeout_graceful_shutdown"], 270)


if __name__ == "__main__":
    unittest.main()
