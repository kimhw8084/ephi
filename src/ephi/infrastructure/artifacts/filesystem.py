"""Safe file-backed reference adapter for immutable artifact bytes.

This is test/reference evidence, not the approved company object-store
binding.  It requires an explicit absolute directory and uses a bounded
maximum artifact size.  Objects are content-addressed by SHA-256 only; no
caller-provided object path is accepted.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import stat
from typing import Iterator
from uuid import uuid4

from ephi.application.artifacts import (
    ArtifactBlobWriteResult,
    ArtifactContentIdentity,
)
from ephi.application.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStorageConfigurationError,
    ArtifactStorageSafetyError,
    ArtifactTooLargeError,
    ArtifactWriteInterruptedError,
    ValidationFailureError,
)


# Explicitly bounded for reference/test use.  Production object-store limits,
# multipart policy and retention are external deployment/qualification work.
DEFAULT_REFERENCE_MAX_ARTIFACT_SIZE = 16 * 1024 * 1024
_LOCK_NAME = ".artifact-publish.lock"
_TEMP_PREFIX = ".artifact-tmp-"


def _configuration(message: str) -> ArtifactStorageConfigurationError:
    return ArtifactStorageConfigurationError(message)


def _safety(message: str, exc: BaseException | None = None) -> ArtifactStorageSafetyError:
    error = ArtifactStorageSafetyError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


class FileArtifactBlobStore:
    """Content-addressed filesystem blob store with no memory fallback."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_artifact_size: int = DEFAULT_REFERENCE_MAX_ARTIFACT_SIZE,
        fault_injector: Callable[[str], object] | None = None,
    ) -> None:
        if not isinstance(root, (str, os.PathLike)):
            raise _configuration("filesystem artifact storage requires an explicit root directory")
        raw_root = os.fspath(root)
        if not isinstance(raw_root, str) or not raw_root.strip() or not os.path.isabs(raw_root):
            raise _configuration("filesystem artifact storage root must be an explicit absolute directory")
        if os.path.lexists(raw_root) and os.path.islink(raw_root):
            raise _configuration("the explicitly configured filesystem artifact root may not be a symlink")
        normalized = os.path.normpath(raw_root)
        # Canonicalize existing parent components (macOS temporary paths often
        # include the system /var symlink), while rejecting a symlink at the
        # configured root itself.  Object paths are still opened relative to
        # the verified root directory handle below.
        root_path = Path(os.path.realpath(normalized))
        if root_path == Path(root_path.anchor) or ".." in root_path.parts:
            raise _configuration("filesystem artifact storage root is too broad or contains traversal")
        if isinstance(max_artifact_size, bool) or not isinstance(max_artifact_size, int) or max_artifact_size <= 0:
            raise _configuration("max_artifact_size must be a positive bounded integer")
        if fault_injector is not None and not callable(fault_injector):
            raise _configuration("fault_injector must be callable or None")

        self.root = root_path
        self.max_artifact_size = max_artifact_size
        self.fault_injector = fault_injector
        self._ensure_root()
        self._root_identity = self._directory_identity()
        self._ensure_lock_file()

    def _ensure_root(self) -> None:
        self._reject_symlink_components(self.root)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _configuration("filesystem artifact storage root could not be created") from exc
        self._reject_symlink_components(self.root)
        try:
            root_stat = os.lstat(self.root)
        except OSError as exc:
            raise _configuration("filesystem artifact storage root could not be inspected") from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise _configuration("filesystem artifact storage root must be a real directory")

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise _configuration("filesystem artifact storage path could not be inspected") from exc
            if stat.S_ISLNK(info.st_mode):
                raise _configuration("filesystem artifact storage root may not contain symlink components")
            if not stat.S_ISDIR(info.st_mode):
                raise _configuration("filesystem artifact storage root contains a non-directory component")

    def _directory_identity(self) -> tuple[int, int]:
        try:
            info = os.lstat(self.root)
        except OSError as exc:
            raise _configuration("filesystem artifact storage root could not be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _configuration("filesystem artifact storage root must be a real directory")
        return info.st_dev, info.st_ino

    def _ensure_lock_file(self) -> None:
        try:
            lock_fd = os.open(
                self.root / _LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            info = os.fstat(lock_fd)
            os.close(lock_fd)
        except OSError as exc:
            raise _configuration("filesystem artifact publish lock could not be created") from exc
        if not stat.S_ISREG(info.st_mode):
            raise _configuration("filesystem artifact publish lock must be a regular file")

    @contextmanager
    def _open_root(self) -> Iterator[int]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            root_fd = os.open(self.root, flags)
        except OSError as exc:
            raise _safety("filesystem artifact root cannot be opened without following a symlink", exc) from exc
        try:
            info = os.fstat(root_fd)
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._root_identity:
                raise _safety("filesystem artifact root changed after adapter initialization")
            yield root_fd
        finally:
            os.close(root_fd)

    @staticmethod
    def _validate_identity(identity: ArtifactContentIdentity) -> None:
        if not isinstance(identity, ArtifactContentIdentity):
            raise ValidationFailureError("identity must be an ArtifactContentIdentity")

    def _validate_read_size(self, identity: ArtifactContentIdentity) -> None:
        if identity.byte_size > self.max_artifact_size:
            raise ArtifactTooLargeError(
                "artifact identity exceeds the reference filesystem size bound",
                details={"max_artifact_size": self.max_artifact_size},
            )

    @contextmanager
    def _publish_lock(self, root_fd: int) -> Iterator[None]:
        try:
            lock_fd = os.open(
                _LOCK_NAME,
                os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
        except OSError as exc:
            raise _safety("filesystem artifact publish lock cannot be opened safely", exc) from exc
        try:
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise _safety("filesystem artifact publish lock is not a regular file")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise _safety("filesystem artifact publish lock cannot be acquired", exc) from exc
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)

    @staticmethod
    def _open_prefix(root_fd: int, identity: ArtifactContentIdentity, *, create: bool) -> int:
        prefix = identity.sha256[:2]
        try:
            info = os.lstat(prefix, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                raise ArtifactNotFoundError("artifact content-addressed object is not present")
            try:
                os.mkdir(prefix, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _safety("artifact object directory could not be created safely", exc) from exc
            try:
                info = os.lstat(prefix, dir_fd=root_fd)
            except OSError as exc:
                raise _safety("artifact object directory could not be inspected", exc) from exc
        except OSError as exc:
            raise _safety("artifact object directory could not be inspected", exc) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _safety("artifact object directory is not a safe real directory")
        try:
            return os.open(prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except OSError as exc:
            raise _safety("artifact object directory cannot be opened without following a symlink", exc) from exc

    @staticmethod
    def _entry_info(prefix_fd: int, name: str) -> os.stat_result | None:
        try:
            info = os.lstat(name, dir_fd=prefix_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _safety("artifact object path could not be inspected", exc) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _safety("artifact object path is not a safe private regular file")
        return info

    def _read_verified_from_prefix(
        self,
        prefix_fd: int,
        identity: ArtifactContentIdentity,
        *,
        missing_is_error: bool = True,
    ) -> bytes:
        name = identity.sha256[2:]
        info = self._entry_info(prefix_fd, name)
        if info is None:
            if missing_is_error:
                raise ArtifactNotFoundError("artifact content-addressed object is not present")
            return b""
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=prefix_fd)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError("artifact content-addressed object is not present") from exc
        except OSError as exc:
            raise _safety("artifact object cannot be opened without following a symlink", exc) from exc
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise _safety("artifact object is not a safe private regular file")
            content = bytearray()
            with os.fdopen(file_fd, "rb", closefd=True) as handle:
                file_fd = -1
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    content.extend(chunk)
                    if len(content) > self.max_artifact_size:
                        raise ArtifactIntegrityError("stored artifact exceeds the reference size bound")
            actual = ArtifactContentIdentity(hashlib.sha256(content).hexdigest(), len(content))
            if actual != identity:
                raise ArtifactIntegrityError(
                    "stored artifact bytes do not match the requested SHA-256 and byte size",
                    details={"sha256": identity.sha256, "byte_size": identity.byte_size},
                )
            return bytes(content)
        finally:
            if file_fd >= 0:
                os.close(file_fd)

    @staticmethod
    def _fsync_directory(directory_fd: int) -> None:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise _safety("filesystem artifact directory durability could not be established", exc) from exc

    def put_bytes(self, content: bytes | bytearray | memoryview) -> ArtifactBlobWriteResult:
        if isinstance(content, (bytearray, memoryview)):
            content = bytes(content)
        if not isinstance(content, bytes):
            raise ValidationFailureError("artifact content must be exact bytes")
        if len(content) > self.max_artifact_size:
            raise ArtifactTooLargeError(
                "artifact content exceeds the reference filesystem size bound",
                details={"max_artifact_size": self.max_artifact_size, "byte_size": len(content)},
            )
        identity = ArtifactContentIdentity(hashlib.sha256(content).hexdigest(), len(content))
        with self._open_root() as root_fd:
            with self._publish_lock(root_fd):
                prefix_fd = self._open_prefix(root_fd, identity, create=True)
                temporary_name: str | None = None
                try:
                    if self._entry_info(prefix_fd, identity.sha256[2:]) is not None:
                        self._read_verified_from_prefix(prefix_fd, identity)
                        return ArtifactBlobWriteResult(identity, True)

                    temporary_name = f"{_TEMP_PREFIX}{uuid4().hex}.tmp"
                    try:
                        temporary_fd = os.open(
                            temporary_name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=root_fd,
                        )
                    except OSError as exc:
                        raise _safety("artifact temporary file could not be created safely", exc) from exc
                    try:
                        offset = 0
                        while offset < len(content):
                            written = os.write(temporary_fd, content[offset:])
                            if written <= 0:  # pragma: no cover - defensive OS contract
                                raise OSError(errno.EIO, "temporary artifact write made no progress")
                            offset += written
                        os.fsync(temporary_fd)
                    except OSError as exc:
                        raise _safety("artifact temporary file could not be flushed safely", exc) from exc
                    finally:
                        os.close(temporary_fd)

                    if self.fault_injector is not None:
                        try:
                            self.fault_injector("before_publish")
                        except ArtifactWriteInterruptedError:
                            raise
                        except Exception as exc:
                            raise ArtifactWriteInterruptedError(
                                "test fault interrupted artifact publication before atomic publish"
                            ) from exc

                    # The lock makes separate adapter instances serialize this
                    # check and rename.  A final object is never rewritten.
                    if self._entry_info(prefix_fd, identity.sha256[2:]) is not None:
                        self._read_verified_from_prefix(prefix_fd, identity)
                        return ArtifactBlobWriteResult(identity, True)
                    try:
                        os.replace(
                            temporary_name,
                            identity.sha256[2:],
                            src_dir_fd=root_fd,
                            dst_dir_fd=prefix_fd,
                        )
                    except OSError as exc:
                        raise _safety("artifact could not be atomically published", exc) from exc
                    temporary_name = None
                    self._fsync_directory(prefix_fd)
                    self._fsync_directory(root_fd)
                    return ArtifactBlobWriteResult(identity, False)
                finally:
                    if temporary_name is not None:
                        try:
                            os.unlink(temporary_name, dir_fd=root_fd)
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            raise _safety("artifact temporary file could not be removed after failed publication", exc) from exc
                    os.close(prefix_fd)

    def verify(self, identity: ArtifactContentIdentity) -> None:
        self._validate_identity(identity)
        self._validate_read_size(identity)
        with self._open_root() as root_fd:
            prefix_fd = self._open_prefix(root_fd, identity, create=False)
            try:
                self._read_verified_from_prefix(prefix_fd, identity)
            finally:
                os.close(prefix_fd)

    def read(self, identity: ArtifactContentIdentity) -> bytes:
        self._validate_identity(identity)
        self._validate_read_size(identity)
        with self._open_root() as root_fd:
            prefix_fd = self._open_prefix(root_fd, identity, create=False)
            try:
                return self._read_verified_from_prefix(prefix_fd, identity)
            finally:
                os.close(prefix_fd)


FilesystemArtifactBlobStore = FileArtifactBlobStore

__all__ = [
    "DEFAULT_REFERENCE_MAX_ARTIFACT_SIZE",
    "FileArtifactBlobStore",
    "FilesystemArtifactBlobStore",
]
