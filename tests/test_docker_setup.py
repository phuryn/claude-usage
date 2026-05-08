"""Static checks for Docker setup (no docker build required — stdlib only)."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestDockerSetup(unittest.TestCase):
    def test_dockerfile_exists_and_uses_python(self):
        df = (ROOT / "Dockerfile").read_text()
        self.assertRegex(df, r"(?m)^FROM\s+python:",
                         "Dockerfile must use a Python base image")

    def test_compose_exposes_dashboard_port(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("8080", compose)

    def test_compose_mounts_claude_home(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertTrue(
            re.search(r"(~|\$HOME|\$\{HOME\})/\.claude", compose),
            f"compose.yaml must bind-mount ~/.claude; got:\n{compose}",
        )


if __name__ == "__main__":
    unittest.main()
