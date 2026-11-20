"""Check verified push publication, directory inventories and binary uploads."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from adbx import AdbClient, AdbError, DirectoryPushResult, PushResult, push, push_file
from adbx.adb import FileInfo, Snapshot, file_digest
from adbx.cli import main


class PushTests(unittest.TestCase):
    """Verify that only complete, stable and verified sources get published."""

    def setUp(self) -> None:
        """Create local sources and a controlled remote transport."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "binary 空 格 ' $; `id`.bin"
        self.data = bytes(range(256)) * 1031
        self.source.write_bytes(self.data)
        self.client = AdbClient()
        self.client._caps = ("crc32", "crc32", True, False)
        self.remote_kind = "missing"
        self.uploads = {}
        self.client._control = Mock(side_effect=self._control)
        self.client.upload_file = Mock(side_effect=self._upload)
        self.client.inspect_file = Mock(side_effect=self._inspect)

    def _control(self, script, timeout=30):
        if "then echo directory;" in script:
            return self.remote_kind
        if "then echo yes;" in script:
            return "no"
        return "ok"

    def _upload(self, source, remote, expected_size, on_chunk):
        data = source.read()
        self.uploads[remote] = data
        on_chunk(data)
        return len(data), file_digest(self.client.hash_algorithm, data)

    def _inspect(self, remote):
        data = self.uploads[remote]
        return Snapshot(FileInfo(len(data), 1, 1, 1, 1),
                        file_digest(self.client.hash_algorithm, data), self.client.hash_algorithm)

    def _published(self):
        return [call.args[0] for call in self.client._control.call_args_list if "mv -T" in call.args[0]]

    def test_file_destination_and_callbacks(self) -> None:
        """Support explicit paths, existing folders and new trailing-slash folders."""
        for kind, remote, expected in (
            ("missing", "/private/new/renamed.bin", "/private/new/renamed.bin"),
            ("directory", "/private/folder", "/private/folder/" + self.source.name),
            ("missing", "/private/new/", "/private/new/" + self.source.name),
        ):
            with self.subTest(kind=kind, remote=remote):
                self.remote_kind = kind
                updates, stages = [], []
                result = push(self.client, self.source, remote, progress=lambda *x: updates.append(x),
                              stage=stages.append)
                self.assertEqual(result, PushResult(expected, len(self.data),
                                                   file_digest("crc32", self.data), "crc32"))
                self.assertEqual(updates[-1], (len(self.data), len(self.data)))
                self.assertTrue(stages)
                self.assertEqual(self.source.read_bytes(), self.data)
                self.assertIn(shlex.quote(expected), self._published()[-1])

    def test_md5_and_empty_file(self) -> None:
        """Verify empty files and the MD5 capability fallback."""
        self.source.write_bytes(b"")
        self.client._caps = ("md5", "md5sum", False, False)
        result = push_file(self.client, self.source, "/private/zero")
        self.assertEqual(result.size, 0)
        self.assertEqual(result.digest, file_digest("md5", b""))

    def test_invalid_remote_and_source(self) -> None:
        """Reject invalid destinations and nonexistent local sources before uploading."""
        for remote in ("relative", "/private/\0bad"):
            with self.subTest(remote=remote), self.assertRaises(AdbError):
                push(self.client, self.source, remote)
        with self.assertRaises(FileNotFoundError):
            push(self.client, self.root / "missing", "/private/file")
        self.client.upload_file.assert_not_called()
        self.assertEqual(self._published(), [])

    def test_verification_failure_does_not_publish(self) -> None:
        """Preserve the destination on wrong size, wrong digest or disconnect."""
        for snapshot in (Snapshot(FileInfo(1, 1, 1, 1, 1), "0" * 8, "crc32"),
                         Snapshot(FileInfo(len(self.data), 1, 1, 1, 1), "0" * 8, "crc32")):
            with self.subTest(snapshot=snapshot):
                self.client.inspect_file.side_effect = None
                self.client.inspect_file.return_value = snapshot
                with self.assertRaises(AdbError):
                    push(self.client, self.source, "/private/file", overwrite=True)
        self.client.upload_file.side_effect = AdbError("disconnected")
        with self.assertRaises(AdbError):
            push(self.client, self.source, "/private/file", overwrite=True)
        self.assertEqual(self._published(), [])
        self.assertTrue(any("rm -rf --" in c.args[0] for c in self.client._control.call_args_list))

    def test_source_mutation_and_cancellation(self) -> None:
        """Discard staged bytes when the source changes or progress cancels."""
        def mutate(copied, total):
            self.source.write_bytes(b"changed")

        with self.assertRaises(AdbError):
            push(self.client, self.source, "/private/file", progress=mutate)
        with self.assertRaises(KeyboardInterrupt):
            push(self.client, self.source, "/private/file",
                 progress=Mock(side_effect=KeyboardInterrupt()))
        self.assertEqual(self._published(), [])

    def test_directory_hidden_empty_and_nested_entries(self) -> None:
        """Include every regular file and empty directory with aggregate byte progress."""
        tree = self.root / "tree"
        (tree / "sub folder" / "empty").mkdir(parents=True)
        (tree / ".hidden").write_bytes(b"hidden")
        (tree / "sub folder" / "zero").touch()
        (tree / "sub folder" / "binary").write_bytes(self.data)
        updates = []
        result = push(self.client, tree, "/private/new/tree", progress=lambda *x: updates.append(x))
        self.assertIsInstance(result, DirectoryPushResult)
        self.assertEqual((result.size, result.file_count), (len(self.data) + 6, 3))
        self.assertEqual(updates[-1], (result.size, result.size))
        self.assertEqual(sorted(self.uploads.values()), sorted([b"", b"hidden", self.data]))
        self.assertEqual(len(self._published()), 1)
        self.assertTrue(any("sub folder/empty" in c.args[0] for c in self.client._control.call_args_list))

    def test_directory_change_or_late_failure_prevents_publication(self) -> None:
        """Reject the complete staged tree if a later file fails or inventory changes."""
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "first").write_bytes(b"first")
        (tree / "last").write_bytes(b"last")
        original = self.client.inspect_file.side_effect
        calls = 0

        def fail_last(remote):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise AdbError("checksum mismatch")
            return original(remote)

        self.client.inspect_file.side_effect = fail_last
        with self.assertRaises(AdbError):
            push(self.client, tree, "/private/tree", overwrite=True)
        self.client.inspect_file.side_effect = original
        with self.assertRaises(AdbError):
            push(self.client, tree, "/private/tree", progress=lambda *_: (tree / "new").touch())
        self.assertEqual(self._published(), [])

    def test_links_rejected_before_upload(self) -> None:
        """Reject a symlink source and a symlink inside a tree."""
        link = self.root / "link"
        try:
            link.symlink_to(self.source)
        except OSError:
            self.skipTest("Creating symlinks requires host privileges")
        for source in (link, self.root):
            with self.subTest(source=source), self.assertRaises(AdbError):
                push(self.client, source, "/private/tree")
        self.client.upload_file.assert_not_called()


class UploadTransportTests(unittest.TestCase):
    """Exercise real subprocess stdin pipes, framing, diagnostics and cleanup."""

    def _upload(self, data, *, mode="upload", expected_size=None, callback=lambda _: None):
        client = AdbClient("custom-adb", "serial")
        client._caps = ("crc32", "crc32", True, False)
        fixture = str(Path(__file__).with_name("stream_fixture.py"))
        processes = []
        original = subprocess.Popen
        client._control = Mock(return_value="uploaded")
        if mode == "upload_unframed":
            client._control.side_effect = AdbError("missing completion marker")

        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        try:
            with patch.object(client, "_command", return_value=[sys.executable, fixture, mode,
                                                               file_digest("crc32", data)]):
                with patch("adbx.adb.subprocess.Popen", side_effect=spawn):
                    return client.upload_file(io.BytesIO(data), "/private/staged",
                                              len(data) if expected_size is None else expected_size, callback)
        finally:
            if processes:
                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(processes[0].stdin.closed)

    def test_binary_empty_and_large_diagnostics(self) -> None:
        """Preserve all byte values without stderr deadlocks, including empty input."""
        for data in (b"", bytes(range(256)) * 32769):
            with self.subTest(size=len(data)):
                self.assertEqual(self._upload(data), (len(data), file_digest("crc32", data)))

    def test_failure_framing_and_size(self) -> None:
        """Reject child failures, missing success markers, short and long inputs."""
        for options in ({"mode": "upload_error"}, {"mode": "upload_unframed"},
                        {"expected_size": 1}, {"expected_size": 10}):
            with self.subTest(options=options), self.assertRaises(AdbError):
                self._upload(b"binary", **options)
        with self.assertRaises(AdbError):
            self._upload(bytes(8 * 1024**2), mode="upload_stop")

    def test_cancellation(self) -> None:
        """Kill and reap an active uploader if its progress callback cancels."""
        with self.assertRaises(KeyboardInterrupt):
            self._upload(bytes(8 * 1024**2), callback=Mock(side_effect=KeyboardInterrupt()))

    def test_binary_exec_in_and_path_quoting(self) -> None:
        """Use binary stdin and preserve quotes and shell metacharacters in paths."""
        client = AdbClient("custom-adb", "serial")
        client._caps = ("crc32", "crc32", True, False)
        remote = "/private/空 格 '; $(touch nope) `id`\n.bin"
        with patch("adbx.adb.subprocess.Popen", side_effect=OSError("test spawn")) as spawn:
            with self.assertRaises(OSError):
                client.upload_file(io.BytesIO(), remote, 0, lambda _: None)
        command = spawn.call_args.args[0]
        self.assertEqual(command[:4], ["custom-adb", "-s", "serial", "exec-in"])
        outer = shlex.split(command[4])
        self.assertEqual(outer[:2], ["su", "-c"])
        self.assertIn(shlex.quote(remote), outer[2])


class PushCliTests(unittest.TestCase):
    """Check push argument order, summaries and failure exit codes."""

    def test_push_summaries_and_options(self) -> None:
        """Pass source/destination, device selection and overwrite into push."""
        for result, expected in (
            (PushResult("/private/file", 12, "abcd", "crc32"), "CRC-32：abcd"),
            (DirectoryPushResult("/private/tree", 12, 3), "檔案數：3"),
        ):
            with self.subTest(result=result):
                output = io.StringIO()
                with patch("adbx.cli.AdbClient") as client, patch("adbx.cli.push", return_value=result) as transfer:
                    with redirect_stdout(output), redirect_stderr(io.StringIO()):
                        code = main(["--adb", "custom-adb", "-s", "serial", "push", "local", "/private/", "--overwrite"])
                self.assertEqual(code, 0)
                self.assertIn(expected, output.getvalue())
                self.assertEqual(transfer.call_args.args, (client.return_value, "local", "/private/"))
                self.assertTrue(transfer.call_args.kwargs["overwrite"])
                client.assert_called_once_with("custom-adb", "serial", jobs=4, compression="auto")

    def test_push_errors(self) -> None:
        """Return CLI errors for transfer failure, missing sources and cancellation."""
        for error, expected in ((AdbError("fail"), 1), (FileNotFoundError("missing"), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(error=error), patch("adbx.cli.push", side_effect=error):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["push", "local", "/private/target"]), expected)
