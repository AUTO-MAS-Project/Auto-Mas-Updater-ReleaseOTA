"""只允许 MaaEnd/ReleaseOTA，覆盖首次创建、版本更新与公开回读。"""

import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from release_ota import publish as publisher

URL = publisher.BASE + "/api/v1/files/maaend/ReleaseOTA/maaend-x64-beta-delta/download"


class DestinationTests(unittest.TestCase):
    def test_official_chunk_client_receives_scoped_id_and_secret_only_in_environment(
        self,
    ):
        with (
            patch.object(
                publisher.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout="uploaded", stderr=""
                ),
            ) as run,
            tempfile.TemporaryDirectory() as directory,
        ):
            source = Path(directory) / "maaend.delta.zip"
            source.write_bytes(b"ZIP")
            publisher.upload_chunks(42, "test-key", source)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--file-id") + 1], "42")
            self.assertEqual(command[command.index("--chunk-mb") + 1], "8")
            self.assertEqual(
                command[command.index("--endpoint") + 1],
                publisher.UPLOAD_BASE + "/api/v1/uploads/",
            )
            self.assertNotIn("test-key", " ".join(command))
            self.assertEqual(run.call_args.kwargs["env"]["AUTOMAS_TOKEN"], "test-key")

    def test_only_four_scoped_public_destinations_are_accepted(self):
        with patch.dict(os.environ, {"AUTOMAS_TOKEN": "test-key"}):
            for key in publisher.FILE_KEYS:
                url = publisher.BASE + f"/api/v1/files/maaend/ReleaseOTA/{key}/download"
                with patch.dict(os.environ, {"AUTOMAS_PUBLIC_URL": url}):
                    self.assertEqual(publisher.target(), (url, "test-key"))
            for url in (
                URL.replace("/maaend/", "/auto-mas/"),
                URL.replace("/ReleaseOTA/", "/Client/"),
                URL.replace("maaend-x64-beta-delta", "other-file"),
                URL + "?file_id=42",
                URL.replace("https://", "http://"),
            ):
                with (
                    self.subTest(url=url),
                    patch.dict(os.environ, {"AUTOMAS_PUBLIC_URL": url}),
                ):
                    with self.assertRaises(publisher.PublishError):
                        publisher.target()

    def test_upload_create_or_version_preserves_zip_and_restricts_scope(self):
        for identifier in (None, 42):

            def execute(command, **kwargs):
                argument = command[command.index("--form") + 1]
                path = Path(argument.removeprefix("file=@").split(";type=")[0])
                self.assertEqual(path.name, "maaend-x64-beta-delta.zip")
                self.assertEqual(path.read_bytes(), b"verified ZIP")
                self.assertTrue(argument.endswith(";type=application/zip"))
                if identifier is None:
                    self.assertIn("project_key=maaend", command)
                    self.assertIn("category_key=ReleaseOTA", command)
                    self.assertIn(publisher.UPLOAD_BASE + "/api/v1/user/files", command)
                else:
                    self.assertIn(
                        publisher.UPLOAD_BASE + "/api/v1/user/files/42/versions",
                        command,
                    )
                # 密钥仅经 stdin 给 curl，不出现在命令参数。
                self.assertNotIn("test-key", " ".join(command))
                Path(command[command.index("--output") + 1]).write_text(
                    json.dumps({"code": 0, "data": {"id": 42}}), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, stdout="200", stderr="")

            with (
                self.subTest(identifier=identifier),
                patch.object(publisher.subprocess, "run", side_effect=execute),
            ):
                self.assertEqual(
                    publisher.upload(
                        identifier,
                        "test-key",
                        b"verified ZIP",
                        filename="maaend-x64-beta-delta.zip",
                    ),
                    {"id": 42},
                )


class PublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_identity_is_checked_before_upload_and_success_readback(self):
        for existing in (True, False):
            record = {
                "project_key": "maaend",
                "category_key": "ReleaseOTA",
                "file_key": "maaend-x64-beta-delta",
            }
            result = {
                **record,
                "id": 42,
                "published_version_no": 2,
                "version": {"version_no": 2, "review_status": "approved"},
            }

            def respond(request):
                return (
                    httpx.Response(200, json={"code": 0, "data": {"file": record}})
                    if existing
                    else httpx.Response(404)
                )

            original = httpx.AsyncClient
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "maaend.delta.zip"
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr(
                        "manifest.json",
                        json.dumps(
                            {
                                "version": 2,
                                "plans": {"old": [1]},
                                "bases": ["old"],
                                "beta_base": "old",
                            }
                        ),
                    )
                    archive.writestr("payload", b"verified ZIP")
                with (
                    patch.dict(
                        os.environ,
                        {"AUTOMAS_FILE_IDS": json.dumps({"maaend-x64-beta-delta": 42})},
                    ),
                    patch.object(publisher, "target", return_value=(URL, "test-key")),
                    patch.object(
                        publisher, "matches", new=AsyncMock(side_effect=[False, True])
                    ),
                    patch.object(publisher, "upload", return_value=result) as upload,
                    patch.object(publisher, "upload_chunks") as chunks,
                    patch.object(
                        httpx,
                        "AsyncClient",
                        side_effect=lambda **kw: original(
                            **kw, transport=httpx.MockTransport(respond)
                        ),
                    ),
                ):
                    await publisher.publish(source)
                    chunks.assert_called_once_with(42, "test-key", source)
                    if existing:
                        upload.assert_not_called()
                    else:
                        upload.assert_called_once()
                        self.assertEqual(upload.call_args.args[:2], (None, "test-key"))
                        with zipfile.ZipFile(
                            io.BytesIO(upload.call_args.args[2])
                        ) as archive:
                            metadata = json.loads(archive.read("manifest.json"))
                            self.assertEqual(metadata["plans"], {})
                            self.assertEqual(metadata["bases"], [])
                            self.assertIsNone(metadata["beta_base"])
                            self.assertEqual(archive.read("payload"), b"")

    async def test_existing_files_use_confirmed_ids_and_allow_overrides(self):
        original = httpx.AsyncClient
        for key, default in (
            ("maaend-arm64-beta-delta", 117),
            ("maaend-x64-stable-delta", 124),
            ("maaend-arm64-stable-delta", 125),
        ):
            url = URL.replace("maaend-x64-beta-delta", key)
            record = {
                "project_key": "maaend",
                "category_key": "ReleaseOTA",
                "file_key": key,
            }
            for overrides, expected in (({}, default), ({key: 42}, 42)):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "bundle.zip"
                    source.write_bytes(b"verified ZIP")
                    with (
                        patch.dict(
                            os.environ,
                            {
                                "AUTOMAS_FILE_IDS": json.dumps(
                                    {"maaend-x64-beta-delta": 116, **overrides}
                                )
                            },
                        ),
                        patch.object(
                            publisher, "target", return_value=(url, "test-key")
                        ),
                        patch.object(
                            publisher,
                            "matches",
                            new=AsyncMock(side_effect=[False, True]),
                        ),
                        patch.object(publisher, "upload") as upload,
                        patch.object(publisher, "upload_chunks") as chunks,
                        patch.object(
                            httpx,
                            "AsyncClient",
                            side_effect=lambda **kw: original(
                                **kw,
                                transport=httpx.MockTransport(
                                    lambda _: httpx.Response(200, json={"data": record})
                                ),
                            ),
                        ),
                    ):
                        await publisher.publish(source)
                        chunks.assert_called_once_with(expected, "test-key", source)
                        upload.assert_not_called()

    async def test_wrong_public_category_prevents_any_upload(self):
        record = {
            "id": 42,
            "project_key": "maaend",
            "category_key": "Other",
            "file_key": "maaend-x64-beta-delta",
        }
        original = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "maaend.delta.zip"
            source.write_bytes(b"verified ZIP")
            with (
                patch.object(publisher, "target", return_value=(URL, "test-key")),
                patch.object(publisher, "matches", new=AsyncMock(return_value=False)),
                patch.object(publisher, "upload") as upload,
                patch.object(publisher, "upload_chunks") as chunks,
                patch.object(
                    httpx,
                    "AsyncClient",
                    side_effect=lambda **kw: original(
                        **kw,
                        transport=httpx.MockTransport(
                            lambda _: httpx.Response(
                                200, json={"code": 0, "data": record}
                            )
                        ),
                    ),
                ),
            ):
                with self.assertRaises(publisher.PublishError):
                    await publisher.publish(source)
                upload.assert_not_called()
                chunks.assert_not_called()
