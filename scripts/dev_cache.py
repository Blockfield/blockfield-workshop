"""Immutable inputs shared by stands; materialize private copy-on-write files."""

import errno
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / ".cache" / "dev-inputs"


def clone_file(source, destination):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise shutil.SameFileError(source, destination)
    if sys.platform.startswith("linux"):
        import fcntl

        try:
            with source.open("rb") as incoming, destination.open("wb") as outgoing:
                fcntl.ioctl(outgoing.fileno(), 0x40049409, incoming.fileno())
        except OSError as error:
            if error.errno not in (
                errno.EXDEV,
                errno.EOPNOTSUPP,
                errno.ENOTTY,
                errno.EINVAL,
                errno.ENOSYS,
            ):
                raise
        else:
            shutil.copystat(source, destination)
            return str(destination)
    return shutil.copy2(source, destination)


def materialize(source, destination):
    """Cache by bytes, keeping each source's permissions on the stand's private file."""
    source = Path(source)
    with source.open("rb") as incoming:
        digest = hashlib.file_digest(incoming, "sha256").hexdigest()
    blob = CACHE / "sha256" / digest
    if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=blob.parent) as temporary:
            pending = Path(temporary) / "blob"
            clone_file(source, pending)
            pending.replace(blob)
    with blob.open("rb") as incoming:
        if hashlib.file_digest(incoming, "sha256").hexdigest() != digest:
            raise RuntimeError(f"Input cache checksum mismatch: {blob}")
    clone_file(blob, destination)
    shutil.copystat(source, destination)
    return str(destination)


def clone_tree(source, destination, **kwargs):
    return shutil.copytree(source, destination, copy_function=clone_file, **kwargs)
