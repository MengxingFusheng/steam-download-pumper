from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .config import PublisherConfig, PublisherSecrets
from .manifest import MAX_MANIFEST_BYTES


class OSSFailure(RuntimeError):
    pass


class OSSClient:
    def __init__(self, config: PublisherConfig, secrets: PublisherSecrets) -> None:
        self.config = config
        self.secrets = secrets

    def _environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "OSS_ACCESS_KEY_ID": self.secrets.oss_access_key_id,
            "OSS_ACCESS_KEY_SECRET": self.secrets.oss_access_key_secret,
            "OSS_REGION": self.config.region,
            "OSS_ENDPOINT": self.config.endpoint,
        }

    def upload(self, source: Path, object_key: str) -> None:
        if not object_key.startswith("pumper/v1/") or ".." in object_key:
            raise OSSFailure("OSS object key is invalid")
        command = [
            self.config.ossutil_path,
            "cp",
            str(source),
            f"oss://{self.config.bucket}/{object_key}",
            "--force",
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._environment(),
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OSSFailure("OSS upload failed") from exc
        if completed.returncode != 0:
            raise OSSFailure("OSS upload failed")

    def read_public(self, relative_key: str) -> bytes:
        if relative_key.startswith("/") or ".." in relative_key:
            raise OSSFailure("public object key is invalid")
        return self.read_url(f"{self.config.public_base_url}/{relative_key}")

    def read_url(self, url: str) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise OSSFailure("public verification requires HTTPS")
        request = urllib.request.Request(
            url,
            headers={"Accept-Encoding": "identity", "User-Agent": "pumper-source-publisher/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                final = urlsplit(response.geturl())
                if final.scheme != "https" or response.status != 200:
                    raise OSSFailure("public verification failed")
                body = response.read(MAX_MANIFEST_BYTES + 1)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise OSSFailure("public verification failed") from exc
        if not body or len(body) > MAX_MANIFEST_BYTES:
            raise OSSFailure("public verification response is invalid")
        return body
