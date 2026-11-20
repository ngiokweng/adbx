"""Push local files and trees into root-protected Android destinations."""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import os
from pathlib import Path
import posixpath
import shlex
import stat
from typing import Callable, Iterator
import uuid

from .adb import AdbClient, AdbError, hash_label


@dataclass(frozen=True)
class PushResult:
    """Record the remote destination, byte count, digest and algorithm of a file."""

    destination: str
    size: int
    digest: str
    algorithm: str


@dataclass(frozen=True)
class DirectoryPushResult:
    """Record a verified remote tree's destination, total bytes and file count."""

    destination: str
    size: int
    file_count: int
    algorithm: str = "crc32"


def _identity(info: os.stat_result) -> tuple[int, ...]:
    # Windows path stat and handle fstat disagree on ctime on some Python
    # versions; size, mtime and file identity remain comparable in both APIs.
    ctime = info.st_ctime_ns if os.name != "nt" else 0
    return (info.st_mode, info.st_size, info.st_mtime_ns, ctime,
            info.st_ino, info.st_dev)


def _source_info(path: Path) -> os.stat_result:
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            or getattr(path, "is_junction", lambda: False)()):
        raise AdbError(f"本機來源包含符號連結、目錄連接點或特殊檔案：{path}")
    return info


def _inventory(root: Path) -> dict[str, os.stat_result]:
    entries = {"": _source_info(root)}

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            info = _source_info(path)
            entries[path.relative_to(root).as_posix()] = info
            if stat.S_ISDIR(info.st_mode):
                visit(path)

    visit(root)
    return entries


def _destination(client: AdbClient, local: Path, remote: str) -> str:
    client._validate_remote(remote)
    quoted = shlex.quote(remote)
    kind = client._control(
        f"if test -d {quoted}; then echo directory; "
        f"elif test -e {quoted} || test -L {quoted}; then echo other; "
        "else echo missing; fi; printf 'ADBX_OK\\n'"
    )
    if kind not in ("directory", "other", "missing"):
        raise AdbError("無法辨識手機目的路徑。")
    if kind == "directory" or remote.endswith("/"):
        if kind == "other":
            raise NotADirectoryError(f"手機目的路徑不是目錄：{remote}")
        if not local.name:
            raise AdbError("推送本機根目錄時，請指定完整的手機目的路徑。")
        return posixpath.join(remote, local.name)
    return remote


def _commands(client: AdbClient, commands: list[str], prelude: str = "") -> None:
    # Keep nested su quoting below Windows' command-line limit for large trees.
    batch: list[str] = []
    length = 0
    for command in commands:
        if batch and length + len(command.encode("utf-8")) > 6000:
            client._control("set -e; " + prelude + "; ".join(batch) + "; printf 'ok\\nADBX_OK\\n'")
            batch, length = [], 0
        batch.append(command)
        length += len(command.encode("utf-8"))
    if batch:
        client._control("set -e; " + prelude + "; ".join(batch) + "; printf 'ok\\nADBX_OK\\n'")


def _check_targets(
    client: AdbClient, destination: str, entries: dict[str, os.stat_result], overwrite: bool,
) -> None:
    root = shlex.quote(destination)
    if not overwrite:
        client._control(
            f"if test -e {root} || test -L {root}; then "
            "echo '手機目的路徑已存在；可用 --overwrite 允許覆寫。' >&2; exit 1; fi; "
            "printf 'ok\\nADBX_OK\\n'"
        )
    prelude = (
        'check() { if test -L "$1"; then echo "目的路徑是符號連結：$1" >&2; exit 1; fi; '
        'if test -e "$1" && ! test "-$2" "$1"; then '
        'echo "目的檔案／目錄類型衝突：$1" >&2; exit 1; fi; }; '
    )
    _commands(client, [
        f"check {shlex.quote(posixpath.join(destination, name) if name else destination)} "
        + ("d" if stat.S_ISDIR(info.st_mode) else "f")
        for name, info in entries.items()
    ], prelude)


@contextmanager
def _staging(client: AdbClient, destination: str) -> Iterator[str]:
    parent = posixpath.dirname(destination)
    work = posixpath.join(parent, f".adbx-push-{uuid.uuid4().hex}.part")
    client._control(
        'test "$(id -u)" = 0 || exit 1; '
        f"umask 077; mkdir -p -- {shlex.quote(parent)} && mkdir -- {shlex.quote(work)} "
        "&& printf 'ready\\nADBX_OK\\n'"
    )
    cleanup = f"rm -rf -- {shlex.quote(work)} && printf 'removed\\nADBX_OK\\n'"
    try:
        yield work + "/content"
    except BaseException:
        # A disconnected device may also reject cleanup; preserve the original
        # failure (including Ctrl+C) so the CLI reports the actual cause.
        with suppress(AdbError, OSError):
            client._control(cleanup)
        raise
    else:
        client._control(cleanup)


def _upload_verified(
    client: AdbClient, local: Path, remote: str, info: os.stat_result,
    consume: Callable[[bytes], None],
) -> tuple[int, str]:
    with local.open("rb") as source:
        if _identity(os.fstat(source.fileno())) != _identity(info):
            raise AdbError(f"本機來源檔案已變動：{local}")
        copied, digest = client.upload_file(source, remote, info.st_size, consume)
        if _identity(os.fstat(source.fileno())) != _identity(info):
            raise AdbError(f"傳輸期間本機來源檔案已變動：{local}")
    snapshot = client.inspect_file(remote)
    if (copied != info.st_size or snapshot.info.size != copied
            or snapshot.algorithm != client.hash_algorithm or snapshot.digest != digest):
        raise AdbError(f"上傳大小或 {hash_label(client.hash_algorithm)} 不一致：{local}")
    if _identity(_source_info(local)) != _identity(info):
        raise AdbError(f"傳輸期間本機來源檔案已變動：{local}")
    return copied, digest


def _move(source: str, destination: str, overwrite: bool) -> str:
    quoted_source = shlex.quote(source)
    return (
        f"mv -T{'f' if overwrite else 'n'} -- {quoted_source} {shlex.quote(destination)}; "
        f"if test -e {quoted_source}; then "
        "echo '手機目的路徑已存在或無法發布。' >&2; exit 1; fi"
    )


def push_file(
    client: AdbClient, local: str | Path, remote: str, *, overwrite: bool = False,
    progress: Callable[[int, int], None] | None = None,
    stage: Callable[[str], None] | None = None,
) -> PushResult:
    """Push local to an absolute remote path and return its verified file result.

    Existing directories receive the local basename; a trailing slash creates
    a directory if needed. Create missing parents using su. progress receives
    copied/total bytes, stage receives phase descriptions. Publish only after
    checksum verification, preserving existing files unless overwrite is set.
    """
    local = Path(local).expanduser().absolute()
    info = _source_info(local)
    if not stat.S_ISREG(info.st_mode):
        raise IsADirectoryError(f"本機來源不是一般檔案：{local}")
    client.ensure_capabilities()
    destination = _destination(client, local, remote)
    entries = {"": info}
    _check_targets(client, destination, entries, overwrite)
    copied = 0

    def consume(chunk: bytes) -> None:
        nonlocal copied
        copied += len(chunk)
        if progress:
            progress(copied, info.st_size)

    with _staging(client, destination) as temporary:
        if stage:
            stage("透過 su 串流上傳至手機暫存檔，並核對校驗值…")
        size, digest = _upload_verified(client, local, temporary, info, consume)
        _check_targets(client, destination, entries, overwrite)
        if stage:
            stage("驗證成功，發布手機目的檔案…")
        _commands(client, [_move(temporary, destination, overwrite)])
    return PushResult(destination, size, digest, client.hash_algorithm)


def push_directory(
    client: AdbClient, local: str | Path, remote: str, *, overwrite: bool = False,
    progress: Callable[[int, int], None] | None = None,
    stage: Callable[[str], None] | None = None,
) -> DirectoryPushResult:
    """Push a local tree to remote and return destination, bytes and file count.

    Include hidden files and empty directories; reject links and special files.
    Verify every staged file before publishing. overwrite merges the verified
    tree, preserving unrelated remote files. Callbacks match push_file.
    """
    local = Path(local).expanduser().absolute()
    if not stat.S_ISDIR(_source_info(local).st_mode):
        raise NotADirectoryError(f"本機來源不是目錄：{local}")
    if stage:
        stage("讀取本機來源目錄清單…")
    entries = _inventory(local)
    client.ensure_capabilities()
    destination = _destination(client, local, remote)
    _check_targets(client, destination, entries, overwrite)
    total = sum(info.st_size for info in entries.values() if stat.S_ISREG(info.st_mode))
    copied = count = 0

    def consume(chunk: bytes) -> None:
        nonlocal copied
        copied += len(chunk)
        if progress:
            progress(copied, total)

    with _staging(client, destination) as temporary:
        _commands(client, [
            f"umask 077; mkdir -p -- {shlex.quote(posixpath.join(temporary, name) if name else temporary)}"
            for name, info in entries.items() if stat.S_ISDIR(info.st_mode)
        ])
        if stage:
            stage("逐檔上傳至手機暫存目錄並核對校驗值…")
        for name, info in entries.items():
            if stat.S_ISREG(info.st_mode):
                _upload_verified(client, local / name, posixpath.join(temporary, name), info, consume)
                count += 1
        after = _inventory(local)
        if ({name: _identity(info) for name, info in entries.items()}
                != {name: _identity(info) for name, info in after.items()}):
            raise AdbError("傳輸期間本機來源目錄已變動。")
        _check_targets(client, destination, entries, overwrite)
        if stage:
            stage("整批驗證成功，發布手機目的資料夾…")
        # A new tree is renamed as a unit. An existing tree is merged only
        # after all files pass verification, leaving unrelated entries intact.
        root = shlex.quote(destination)
        exists = client._control(
            f"if test -d {root}; then echo yes; else echo no; fi; printf 'ADBX_OK\\n'"
        )
        if exists == "no":
            _commands(client, [_move(temporary, destination, False)])
        elif exists == "yes" and overwrite:
            _commands(client, [
                f"umask 077; mkdir -p -- {shlex.quote(posixpath.join(destination, name))}"
                if stat.S_ISDIR(info.st_mode) else
                _move(posixpath.join(temporary, name), posixpath.join(destination, name), True)
                for name, info in entries.items() if name
            ])
        else:
            raise AdbError("手機目的目錄已變動，無法發布。")
    return DirectoryPushResult(destination, copied, count, client.hash_algorithm)


def push(
    client: AdbClient, local: str | Path, remote: str, *, overwrite: bool = False,
    progress: Callable[[int, int], None] | None = None,
    stage: Callable[[str], None] | None = None,
) -> PushResult | DirectoryPushResult:
    """Detect local's type and push to remote with overwrite and optional callbacks."""
    path = Path(local).expanduser()
    info = _source_info(path)
    transfer = push_directory if stat.S_ISDIR(info.st_mode) else push_file
    return transfer(client, path, remote, overwrite=overwrite, progress=progress, stage=stage)
