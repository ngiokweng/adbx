"""Run opt-in push checks in a disposable, root-only Android workspace."""

from dataclasses import replace
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch
import uuid

from adbx import AdbClient, AdbError, pull_file, push


@unittest.skipUnless(os.environ.get("ADBX_DEVICE_TESTS") == "1", "real-device tests are opt-in")
class PushDeviceTests(unittest.TestCase):
    """Exercise real root writes without touching existing application data."""

    def setUp(self) -> None:
        """Allocate unique local and root-owned remote directories for each test."""
        self.client = AdbClient(os.environ.get("ADB", "adb"), os.environ.get("ANDROID_SERIAL"))
        directory = tempfile.TemporaryDirectory(prefix="adbx-push-smoke-")
        self.addCleanup(directory.cleanup)
        self.local = Path(directory.name)
        self.remote = f"/data/local/tmp/adbx-push-smoke-{uuid.uuid4().hex}"
        self._root(f"umask 077; mkdir -- {shlex.quote(self.remote)}")
        self.addCleanup(self._root, f"rm -rf -- {shlex.quote(self.remote)}")

    def _root(self, script):
        self.client._control(f"set -e; {script}; printf 'ok\\nADBX_OK\\n'")

    def _read(self, remote):
        target = self.local / "received.bin"
        pull_file(self.client, remote, target, overwrite=True)
        return target.read_bytes()

    def _assert_clean(self):
        remaining = self.client._run(self.client._root_command(
            f"find {shlex.quote(self.remote)} -name '.adbx-push-*.part' -print"
        ), timeout=30)
        self.assertEqual(remaining, b"")

    def test_binary_file_and_new_parent_directories(self) -> None:
        """Create arbitrary root-only parents and preserve binary bytes and quoted paths."""
        source = self.local / "binary 空 格 ' $; `id`.bin"
        data = os.urandom(1024**2 + 137) + bytes(range(256))
        source.write_bytes(data)
        remote = self.remote + "/new/sub/空 格 ' $; `id`\n.bin"
        result = push(self.client, source, remote)
        self.assertEqual(result.destination, remote)
        self.assertEqual(result.size, len(data))
        self.assertEqual(self._read(remote), data)
        self.assertEqual(source.read_bytes(), data)
        self._assert_clean()

    def test_directory_hidden_empty_and_verified_merge(self) -> None:
        """Merge only verified trees and preserve unrelated remote files and old data on failure."""
        tree = self.local / "tree"
        (tree / "sub folder" / "empty").mkdir(parents=True)
        (tree / "sub folder" / "binary.status").mkdir()
        (tree / ".hidden").write_bytes(b"old hidden")
        (tree / "sub folder" / "zero").touch()
        (tree / "sub folder" / "binary").write_bytes(bytes(range(256)))
        result = push(self.client, tree, self.remote)
        self.assertEqual((result.file_count, result.size), (3, 266))
        self.assertEqual(
            {(entry.path, entry.is_dir) for entry in self.client.list_directory(result.destination)},
            {(path.relative_to(tree).as_posix(), path.is_dir()) for path in tree.rglob("*")},
        )
        self._root(f"test -d {shlex.quote(result.destination + '/sub folder/empty')}; "
                   f"printf keep > {shlex.quote(result.destination + '/unrelated')}")
        (tree / ".hidden").write_bytes(b"new hidden")
        with self.assertRaises(AdbError):
            push(self.client, tree, self.remote)
        original = self.client.inspect_file

        def corrupt_last(remote):
            snapshot = original(remote)
            wrong = ("f" if snapshot.digest[0] == "0" else "0") + snapshot.digest[1:]
            return replace(snapshot, digest=wrong) if remote.endswith("/zero") else snapshot

        with patch.object(self.client, "inspect_file", side_effect=corrupt_last):
            with self.assertRaises(AdbError):
                push(self.client, tree, self.remote, overwrite=True)
        self.assertEqual(self._read(result.destination + "/.hidden"), b"old hidden")
        push(self.client, tree, self.remote, overwrite=True)
        self.assertEqual(self._read(result.destination + "/.hidden"), b"new hidden")
        self.assertEqual(self._read(result.destination + "/unrelated"), b"keep")
        self._assert_clean()

    def test_empty_sources_and_trailing_slash(self) -> None:
        """Upload zero-byte files and empty trees into new directory destinations."""
        source = self.local / "zero"
        source.touch()
        result = push(self.client, source, self.remote + "/new/")
        self.assertEqual(result.destination, self.remote + "/new/zero")
        self.assertEqual(self._read(result.destination), b"")
        tree = self.local / "empty"
        tree.mkdir()
        result = push(self.client, tree, self.remote + "/renamed")
        self.assertEqual((result.file_count, result.size), (0, 0))
        self._root(f"test -d {shlex.quote(result.destination)}")
        self._assert_clean()

    def test_overwrite_and_cancellation(self) -> None:
        """Keep the previous file on rejection or cancellation and replace it on success."""
        source = self.local / "file.bin"
        source.write_bytes(b"old")
        result = push(self.client, source, self.remote)
        source.write_bytes(os.urandom(8 * 1024**2))
        with self.assertRaises(AdbError):
            push(self.client, source, self.remote)

        def cancel(copied, total):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            push(self.client, source, self.remote, overwrite=True, progress=cancel)
        self.assertEqual(self._read(result.destination), b"old")
        self._assert_clean()
        push(self.client, source, self.remote, overwrite=True)
        self.assertEqual(self._read(result.destination), source.read_bytes())
        self._assert_clean()

    def test_destination_links_and_type_conflicts(self) -> None:
        """Reject target links and file/directory conflicts before sending source bytes."""
        tree = self.local / "tree"
        tree.mkdir()
        (tree / "file").write_bytes(b"new")
        target = self.remote + "/tree"
        outside = self.remote + "/outside"
        self._root(f"mkdir -- {shlex.quote(outside)}; "
                   f"ln -s {shlex.quote(outside)} {shlex.quote(target)}")
        with patch.object(self.client, "upload_file") as upload:
            with self.assertRaises(AdbError):
                push(self.client, tree, self.remote, overwrite=True)
            self._root(f"rm -- {shlex.quote(target)}; "
                       f"mkdir -p -- {shlex.quote(target + '/file')}")
            with self.assertRaises(AdbError):
                push(self.client, tree, self.remote, overwrite=True)
            upload.assert_not_called()
        self._assert_clean()

    def test_shared_storage(self) -> None:
        """Write to shared storage without requiring hardlinks or a fixed staging mount."""
        shared = f"/sdcard/adbx-push-smoke-{uuid.uuid4().hex}"
        self._root(f"mkdir -- {shlex.quote(shared)}")
        self.addCleanup(self._root, f"rm -rf -- {shlex.quote(shared)}")
        source = self.local / "shared.bin"
        data = bytes(range(256)) * 4097
        source.write_bytes(data)
        result = push(self.client, source, shared + "/nested/")
        self.assertEqual(result.destination, shared + "/nested/shared.bin")
        self.assertEqual(self._read(result.destination), data)
        self._root(f"test -z \"$(find {shlex.quote(shared)} -name '.adbx-push-*.part' -print)\"")
