from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from download_dependencies import DependencyError, ensure_dependencies, load_bom  # noqa: E402
from project_config import load_config  # noqa: E402


class DownloadDependencyTests(unittest.TestCase):
    def test_dependency_is_downloaded_and_checksum_verified(self) -> None:
        payload = b"pinned test dependency\n"
        expected = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            source = temporary / "source.deb"
            source.write_bytes(payload)
            bom = temporary / "bom.json"
            bom.write_text(
                json.dumps(
                    {
                        "schema": "vyos.ova.builder.bom/v1",
                        "dependencies": [
                            {
                                "name": "syft",
                                "version": "test",
                                "filename": "syft.deb",
                                "url": source.as_uri(),
                                "sha256": expected,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = copy.deepcopy(load_config(environ={}))
            config["paths"]["artifacts"] = str(temporary / "artifacts")

            resolved = ensure_dependencies(config, bom_path=bom)
            self.assertEqual(resolved["syft"].read_bytes(), payload)

            resolved["syft"].write_bytes(b"corrupt")
            repaired = ensure_dependencies(config, bom_path=bom)
            self.assertEqual(repaired["syft"].read_bytes(), payload)

    def test_bom_filename_cannot_escape_download_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            bom = Path(temporary_name) / "bom.json"
            bom.write_text(
                json.dumps(
                    {
                        "schema": "vyos.ova.builder.bom/v1",
                        "dependencies": [
                            {
                                "name": "syft",
                                "version": "test",
                                "filename": "../syft.deb",
                                "url": "https://example.invalid/syft.deb",
                                "sha256": "0" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DependencyError, "must not contain a path"):
                load_bom(bom)


if __name__ == "__main__":
    unittest.main()
