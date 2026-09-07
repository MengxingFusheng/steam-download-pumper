import base64
import importlib.util
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch


class FleetWebProxyTests(unittest.TestCase):
    def _module(self, directory: str):
        password_file = Path(directory) / "password"
        password_file.write_text("a-long-random-password\n", encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "deploy" / "fleet_web_proxy.py"
        spec = importlib.util.spec_from_file_location("fleet_web_proxy_test", script)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(
            os.environ,
            {"WEB_AUTH_ENABLED": "true", "WEB_AUTH_USERNAME": "operator", "WEB_AUTH_PASSWORD_FILE": str(password_file)},
            clear=False,
        ):
            assert spec.loader is not None
            spec.loader.exec_module(module)
        return module

    def test_basic_auth_requires_exact_username_and_password(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self._module(directory)
            handler = object.__new__(module.ProxyHandler)
            handler.headers = Message()
            self.assertFalse(handler._authorized())

            token = base64.b64encode(b"operator:a-long-random-password").decode("ascii")
            handler.headers["authorization"] = f"Basic {token}"
            self.assertTrue(handler._authorized())

            handler.headers.replace_header("authorization", "Basic !!!")
            self.assertFalse(handler._authorized())

    def test_authentication_can_be_disabled_for_a_private_tunnel(self):
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "missing-password"
            script = Path(__file__).resolve().parents[1] / "deploy" / "fleet_web_proxy.py"
            spec = importlib.util.spec_from_file_location("fleet_web_proxy_no_auth_test", script)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(
                os.environ,
                {"WEB_AUTH_ENABLED": "false", "WEB_AUTH_PASSWORD_FILE": str(password_file)},
                clear=False,
            ):
                assert spec.loader is not None
                spec.loader.exec_module(module)
            handler = object.__new__(module.ProxyHandler)
            handler.headers = Message()
            self.assertTrue(handler._authorized())

    def test_proxy_caps_forwarded_request_size(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self._module(directory)
            self.assertGreaterEqual(module.MAX_REQUEST_BYTES, 1_048_576)
            self.assertLess(module.MAX_REQUEST_BYTES, 2_000_000)
