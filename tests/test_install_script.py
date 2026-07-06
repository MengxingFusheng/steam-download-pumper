import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallScriptTests(unittest.TestCase):
    def test_install_script_is_non_interactive_and_runs_compose(self):
        script = ROOT / "install.sh"

        self.assertTrue(script.exists())
        content = script.read_text(encoding="utf-8")
        self.assertIn("pick_lan_ip", content)
        self.assertIn("pick_lan_ips", content)
        self.assertIn("EGRESS_MODE", content)
        self.assertIn("LAN_IPS", content)
        self.assertIn("LAN_IPS 数量必须等于 LINE_COUNT", content)
        self.assertIn("STEAM_PUMPER_REPO_URL", content)
        self.assertIn("git clone", content)
        self.assertIn("docker compose up -d --build", content)
        self.assertNotIn("read -r -p", content)

    def test_install_script_has_valid_bash_syntax(self):
        result = subprocess.run(["bash", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
