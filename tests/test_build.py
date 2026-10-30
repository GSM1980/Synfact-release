"""Offline tests for the build contract; no Elektron firmware fixtures required."""
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
import zlib

import importlib.util

# Browser parity tests also import the desktop directory, which has build.py.
_spec = importlib.util.spec_from_file_location('release_build_under_test', Path(__file__).resolve().parents[1] / 'build.py')
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def spec(data, filename):
    return {"filename": filename, "bytes": len(data), "sha256": build.sha256(data)}


def make_patch(original, modified):
    return build.MAGIC + struct.pack(">I", len(modified)) + zlib.compress(bytes(a ^ b for a, b in zip(original, modified)))


class Response(io.BytesIO):
    url = "https://example.invalid/machines.smz4"

    def geturl(self):
        return self.url


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "release").mkdir()
        self.original = bytes(range(256)) * 4
        self.modified = b"modified release" + self.original[16:900]
        self.machines = b"downloaded unchanged machines"
        self.delta = make_patch(self.original, self.modified)
        self.original_path = self.root / "arbitrary-name.syx"
        self.original_path.write_bytes(self.original)
        self.output = self.root / "output"
        self.manifest = {
            "original": spec(self.original, "official.syx"),
            "firmware": spec(self.modified, "modified.syx"),
            "patch": spec(self.delta, "firmware.xor.zlib"),
            "machines": {**spec(self.machines, "machines.smz4"), "url": Response.url},
        }
        self.save_manifest()
        (self.root / "release/firmware.xor.zlib").write_bytes(self.delta)
        self.root_patch = patch.object(build, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def save_manifest(self):
        (self.root / "release/manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def run_build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return build.build(self.original_path, self.output)

    def test_complete_build_exact_bytes_and_arbitrary_input_name(self):
        with patch.object(build, "urlopen", return_value=Response(self.machines)) as download:
            self.assertEqual(self.run_build(), self.output)
        self.assertEqual((self.output / "modified.syx").read_bytes(), self.modified)
        self.assertEqual((self.output / "machines.smz4").read_bytes(), self.machines)
        self.assertEqual(json.loads((self.output / "build-info.json").read_text()), self.manifest)
        self.assertEqual(self.original_path.read_bytes(), self.original)
        self.assertEqual(download.call_args.args[0].full_url, Response.url)
        self.assertFalse(list(self.root.glob(".synfact-build-*")))

    def test_wrong_original_rejected_before_network(self):
        for content in (b"OS 1.42", self.original[:-1], bytes([255]) + self.original[1:]):
            with self.subTest(content_length=len(content)):
                self.original_path.write_bytes(content)
                with patch.object(build, "urlopen") as download, self.assertRaises(build.BuildError):
                    self.run_build()
                download.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_bad_patch_rejected_before_network(self):
        (self.root / "release/firmware.xor.zlib").write_bytes(self.delta[:-1])
        with patch.object(build, "urlopen") as download, self.assertRaises(build.BuildError):
            self.run_build()
        download.assert_not_called()

    def test_wrong_generated_hash_rejected_before_network(self):
        self.manifest["firmware"]["sha256"] = "0" * 64
        self.save_manifest()
        with patch.object(build, "urlopen") as download, self.assertRaises(build.BuildError):
            self.run_build()
        download.assert_not_called()

    def test_download_failure_has_no_partial_output(self):
        with patch.object(build, "urlopen", side_effect=URLError("offline")), self.assertRaises(URLError):
            self.run_build()
        self.assertFalse(self.output.exists())

    def test_wrong_truncated_oversized_or_html_download_rejected(self):
        for data in (b"<html>Login</html>", self.machines[:-1], self.machines + b"extra", b"X" * len(self.machines)):
            with self.subTest(data=data):
                with patch.object(build, "urlopen", return_value=Response(data)), self.assertRaises(build.BuildError):
                    self.run_build()
                self.assertFalse(self.output.exists())

    def test_output_exists_preserves_content(self):
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("keep")
        with patch.object(build, "urlopen") as download, self.assertRaises(build.BuildError):
            self.run_build()
        download.assert_not_called()
        self.assertEqual(sentinel.read_text(), "keep")

    def test_invalid_urls_rejected(self):
        for url in (None, "http://example.invalid/machines", "file:///tmp/file"):
            self.manifest["machines"]["url"] = url
            self.save_manifest()
            with patch.object(build, "urlopen") as download, self.assertRaises(build.BuildError):
                self.run_build()
            download.assert_not_called()

    def test_insecure_redirect_rejected(self):
        response = Response(self.machines)
        response.url = "http://example.invalid/file"
        with patch.object(build, "urlopen", return_value=response), self.assertRaises(build.BuildError):
            self.run_build()
        self.assertFalse(self.output.exists())

    def test_path_traversal_rejected_before_network(self):
        self.manifest["firmware"]["filename"] = "../escaped.syx"
        self.save_manifest()
        with patch.object(build, "urlopen") as download, self.assertRaises(build.BuildError):
            self.run_build()
        download.assert_not_called()

    def test_patch_decoder_rejects_malformed_data(self):
        header = build.MAGIC + struct.pack(">I", len(self.modified))
        for data in (b"", b"WRONG" + self.delta, self.delta[:-1], self.delta + b"trailing",
                     header + zlib.compress(b"X" * (len(self.modified) + 1)),
                     header + b"bad zlib",
                     build.MAGIC + struct.pack(">I", len(self.original) + 1) + zlib.compress(b"")):
            with self.subTest(length=len(data)), self.assertRaises(build.BuildError):
                build.apply_patch(self.original, data, len(self.modified))


if __name__ == "__main__":
    unittest.main()
