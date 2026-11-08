"""Pinned project-local quality tools and reproducible build Python.

Core downloader: the single pin set lives in scripts/tools.lock.json and this
module only reads it. Every download is retried with backoff, written
atomically, and verified against the locked hash. `BF_TOOL_CACHE` points all
tools at one shared cache directory (hook-test fixtures and fresh worktrees
use it); `toolchain.py --tools a,b` prefetches named tools for CI.

Workspace-only code (Temurin JDK, boat CLI, Debian Xvfb/VirtualGL) lives in
scripts/toolchain_local.py and is re-exported here, so existing imports keep
working. Other repos vendor this file plus the lock, with their own local
module next to it.
"""

import base64
import contextlib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

__all__ = [
    "CACHE",
    "DEV_CACHE",
    "LOCK_PATH",
    "cache_dir",
    "call",
    "canonical_python",
    "check_hash",
    "child_env",
    "download",
    "fetch",
    "host",
    "jdk_env",
    "load_lock",
    "lock_platform",
    "lock_tool",
    "native",
    "pinned_bins",
    "read_url",
    "replace",
    "retried",
    "ruff_version",
    "tool",
    # Re-exported from scripts/toolchain_local.py (workspace-only pins).
    "JAVA_ASSETS",
    "JAVA_VERSION",
    "check_java",
    "deb_libraries",
    "deb_payload",
    "java_home",
]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from terminal import progress  # noqa: E402
from dev_cache import CACHE as DEV_CACHE, clone_tree  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "quality"
LOCK_PATH = ROOT / "scripts" / "tools.lock.json"


def load_lock():
    """The single pin set every repo vendors: tool, version, per-platform
    sources in try order, and the hash each source must verify against."""
    try:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except OSError as error:
        raise RuntimeError(f"Cannot read tool pins at {LOCK_PATH}: {error}")
    if lock.get("version") != 1 or "tools" not in lock:
        raise RuntimeError(f"Unsupported tool pins at {LOCK_PATH}: expected version 1")
    return lock


def cache_dir():
    """Shared tool cache: BF_TOOL_CACHE when set (hook-test fixtures and
    fresh worktrees point it at the parent checkout's cache), else this
    checkout's .cache/quality."""
    override = os.environ.get("BF_TOOL_CACHE")
    return Path(override) if override else CACHE


def lock_tool(name, lock=None):
    try:
        return (lock or load_lock())["tools"][name]
    except KeyError:
        raise RuntimeError(
            f"Unknown tool {name!r}: not pinned in scripts/tools.lock.json"
        )


def lock_platform(name, platform=None, lock=None):
    """Locked entry, resolved platform key (aliases, then platform-independent
    "any"), and its source spec for this host."""
    lock = lock or load_lock()
    entry = lock_tool(name, lock)
    key = platform or "-".join(host())
    platforms = entry["platforms"]
    if key not in platforms:
        key = entry.get("aliases", {}).get(key, "any")
    try:
        return entry, key, platforms[key]
    except KeyError:
        raise RuntimeError(f"No {key} asset pinned for tool {name!r}")


def check_hash(data, spec, name):
    """Verify downloaded bytes against the locked hash (sha256, or the
    registry integrity value for npm tarballs)."""
    if "sha256" in spec:
        if hashlib.sha256(data).hexdigest() != spec["sha256"]:
            raise RuntimeError(f"Tool checksum mismatch: {name}")
    elif "integrity" in spec:
        algorithm, _, encoded = spec["integrity"].partition("-")
        try:
            expected = base64.b64decode(encoded)
            digest = hashlib.new(algorithm, data).digest()
        except (ValueError, TypeError) as error:
            raise RuntimeError(f"Tool checksum mismatch: {name} ({error})")
        if digest != expected:
            raise RuntimeError(f"Tool checksum mismatch: {name}")
    else:
        raise RuntimeError(f"No hash pinned for tool {name!r}")


def child_env():
    env = os.environ.copy()
    for key in (
        "APPIMAGE",
        "__MISE_SHIM",
        "__MISE_DIFF",
        "PYTHONHOME",
        "PYTHONPATH",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "CLASSPATH",
    ):
        env.pop(key, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    sdk = Path(env.get("JAVA_HOME", "")) / "bin"
    env["PATH"] = os.pathsep.join(
        [
            *([str(sdk)] if env.get("JAVA_HOME") and sdk.is_dir() else []),
            *pinned_bins(),
            *(
                entry
                for entry in env.get("PATH", "").split(os.pathsep)
                if not (
                    Path(entry).name.lower() == "shims"
                    and Path(entry).parent.name.lower() == "mise"
                )
            ),
        ]
    )
    return env


def jdk_env():
    """Env with the pinned Temurin 21 first: every Gradle/Java entrypoint uses
    this instead of inheriting whatever java the host PATH happens to carry."""
    environment = child_env()
    sdk = str(java_home())
    environment["JAVA_HOME"] = sdk
    entries = [str(Path(sdk) / "bin"), *environment.get("PATH", "").split(os.pathsep)]
    environment["PATH"] = os.pathsep.join(
        dict.fromkeys(part for part in entries if part)
    )
    return environment


def native(name):
    java_tool = Path(name).name.lower()
    if java_tool in (
        "java",
        "java.exe",
        "javac",
        "javac.exe",
        "jdb",
        "jdb.exe",
        "gradlew",
        "gradlew.bat",
    ):
        sdk = java_home()
        environment = child_env()
        environment["JAVA_HOME"] = str(sdk)
        environment["PATH"] = os.pathsep.join(
            dict.fromkeys([str(sdk / "bin"), *environment["PATH"].split(os.pathsep)])
        )
        os.environ.clear()
        os.environ.update(environment)
        if java_tool not in ("gradlew", "gradlew.bat"):
            extension = ".exe" if host()[0] == "windows" else ""
            name = sdk / "bin" / (java_tool.removesuffix(".exe") + extension)
    executable = shutil.which(str(name), path=child_env()["PATH"])
    if executable is None:
        raise RuntimeError(
            f"Missing native {name}; install the repository's documented SDK prerequisites"
        )
    return executable


def call(*args, capture=False):
    executable = native(args[0])
    return subprocess.run(
        [executable, *map(str, args[1:])],
        check=True,
        env=child_env(),
        text=True,
        stdout=subprocess.PIPE if capture else sys.stderr,
        shell=os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"),
    ).stdout


def host():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if system not in ("linux", "windows") or arch is None:
        raise RuntimeError(f"Unsupported platform: {system}/{machine}")
    return system, arch


def retried(fetch, attempts=3):
    """Runners hit transient GitHub/R2 failures (HTTP 5xx, read timeouts); checksums still verify the bytes."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch()
        except (OSError, subprocess.SubprocessError) as error:
            permanent = (
                isinstance(error, urllib.error.HTTPError)
                and error.code < 500
                and error.code != 429
            )
            if permanent or attempt == attempts:
                raise
            print(
                f"download failed ({error}), retry {attempt}/{attempts - 1}",
                file=sys.stderr,
            )
            time.sleep(10 * attempt)


def read_url(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def replace(target, data, mode=None):
    """Publish bytes so concurrent checkouts never see a partial file (and a
    running binary is never rewritten in place): sibling temp file, then rename
    into place. Mode is applied when given (tool binaries need 0o755)."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=".dl-", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if mode is not None:
            os.chmod(temporary, mode)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


CANONICAL_PYTHON_PROBE = "(3, 12, 14) 1.3.2 1.3.2"


def download(name, url, checksum, *, cache=None):
    """Single pinned file: cached copy verified, else downloaded with retries
    and published atomically. Kept for explicit callers; lock-driven code
    uses fetch() for ordered source fallback."""
    root = Path(cache) if cache is not None else cache_dir()
    target = root / name
    if target.is_file():
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != checksum:
            raise RuntimeError(
                f"Tool checksum mismatch: {name} (delete {target} and retry)"
            )
        return target
    with progress(f"Downloading {name}"):
        data = retried(lambda: read_url(url))
    if hashlib.sha256(data).hexdigest() != checksum:
        raise RuntimeError(f"Tool checksum mismatch: {name}")
    replace(target, data)
    return target


def fetch(tool_name, filename=None, *, cache=None, platform=None):
    """Locked tool file: each source tried in lock order, every source
    retried and hash-verified."""
    lock = load_lock()
    entry, key, spec = lock_platform(tool_name, platform, lock)
    urls = list(spec["urls"])
    name = filename or Path(urls[0]).name
    root = Path(cache) if cache is not None else cache_dir()
    target = root / name
    if target.is_file():
        try:
            check_hash(target.read_bytes(), spec, name)
        except RuntimeError as error:
            raise RuntimeError(f"{error} (delete {target} and retry)")
        return target
    failures = []
    for url in urls:
        try:
            with progress(f"Downloading {name}"):
                data = retried(lambda url=url: read_url(url))
        except (OSError, subprocess.SubprocessError) as error:
            failures.append(f"{url}: {error}")
            continue
        try:
            check_hash(data, spec, name)
        except RuntimeError as error:
            failures.append(f"{url}: {error}")
            continue
        replace(target, data)
        return target
    raise RuntimeError(f"Tool {tool_name!r} download failed: " + "; ".join(failures))


def _pin_key(spec):
    """Cache subdirectory for one locked asset: the first 16 hex digits of
    its pin, so a lock bump never reuses (or flips) another pin's binaries."""
    if "sha256" in spec:
        return spec["sha256"][:16]
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def pinned_bins():
    """Every `bin/<pin>` directory of this checkout's lock for the current
    host. Child processes and CI resolve bare tool names from these, so no
    shared `bin/<name>` link can flip between pins under a shared cache."""
    try:
        lock = load_lock()
    except RuntimeError:
        return []
    directories = []
    for name in lock.get("tools", {}):
        try:
            _, _, spec = lock_platform(name, lock=lock)
        except RuntimeError:
            continue  # no asset pinned for this host
        directories.append(str(cache_dir() / "bin" / _pin_key(spec)))
    return directories


@contextlib.contextmanager
def _held(lock_path, timeout=600):
    """Exclusive OS file lock beside an install dir (flock on POSIX,
    msvcrt.locking on Windows). The OS releases it if the holder dies, so a
    crashed builder can never wedge later runs; only the holder may
    delete or rebuild the guarded dir."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(lock_path, "a+b") as handle:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Tool install is locked by another process ({lock_path}); "
                        f"gave up after {timeout}s"
                    )
                time.sleep(1)
        try:
            yield handle
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _mark_complete(directory):
    """Completion marker written last: an install dir counts as usable only
    when its builder finished and left this file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".complete").write_text("complete\n", encoding="utf-8")


def ruff_version(python):
    try:
        probe = subprocess.run(
            [str(python), "-I", "-m", "ruff", "--version"],
            env=child_env(),
            capture_output=True,
            text=True,
        )
    except OSError:  # interpreter missing or a dangling symlink
        return ""
    return "" if probe.returncode else probe.stdout.strip()


def _install_ruff_venv(directory, python, expected):
    try:
        call(canonical_python(), "-I", "-m", "venv", directory)
        # The wheel is hash-verified by fetch(); pip installs the local file.
        wheel = fetch("ruff")
        # pip does not retry a read timeout in the middle of a download.
        retried(
            lambda: call(
                python,
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            )
        )
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    if ruff_version(python) != expected:
        shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError(f"ruff install did not produce {expected}")
    _mark_complete(directory)


def tool_ruff():
    # Keyed by the locked version, like tool_npm: a lock bump must not reuse
    # (or reinstall under) another pin's venv.
    version = lock_tool("ruff")["version"]
    expected = "ruff " + version
    directory = cache_dir() / ("venv-ruff-" + version)
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    ruff = directory / ("Scripts/ruff.exe" if os.name == "nt" else "bin/ruff")
    marker = directory / ".complete"
    if marker.is_file() and ruff_version(python) == expected:
        return ruff
    with _held(cache_dir() / (directory.name + ".lock")):
        if marker.is_file() and ruff_version(python) == expected:
            return ruff  # a lock holder finished while we waited
        if directory.exists():
            # Only the lock holder reaches here, so no live builder or runner
            # can be using this unmarked dir.
            shutil.rmtree(directory, ignore_errors=True)
        _install_ruff_venv(directory, python, expected)
    return ruff


def tool_npm(name):
    # Keyed by the locked version, like canonical_python: a lock bump must not reuse an old install.
    directory = cache_dir() / ("npm-" + name + "-" + lock_tool(name)["version"])
    binary = (
        directory
        / "node_modules"
        / ".bin"
        / (name + ".cmd" if os.name == "nt" else name)
    )
    marker = directory / ".complete"
    if marker.is_file() and binary.is_file():
        return binary
    with _held(cache_dir() / (directory.name + ".lock")):
        if marker.is_file() and binary.is_file():
            return binary  # a lock holder finished while we waited
        if directory.exists():
            # Only the lock holder reaches here, so no live npm run can be
            # using this unmarked dir.
            shutil.rmtree(directory, ignore_errors=True)
        # The tarball carries the registry integrity hash; npm installs the
        # verified local file, so no registry round-trip can swap the bytes.
        tarball = fetch(name)
        call(
            "npm",
            "install",
            "--prefix",
            directory,
            "--no-audit",
            "--no-fund",
            "--ignore-scripts",
            "--package-lock=false",
            "--save-exact",
            str(tarball),
        )
        _mark_complete(directory)
    return binary


def tool(name):
    if name == "ruff":
        return tool_ruff()
    if name in ("prettier", "pnpm"):
        return tool_npm(name)
    lock = load_lock()
    if name not in lock["tools"]:
        from toolchain_local import local_tool

        return local_tool(name)
    system, arch = host()
    _, _, spec = lock_platform(name, lock=lock)
    archive = fetch(name, name + "-" + Path(spec["urls"][0]).name)
    filename = name + (".exe" if system == "windows" else "")
    if spec["kind"] == "zip":
        with zipfile.ZipFile(archive) as source:
            member = next(n for n in source.namelist() if Path(n).name == filename)
            data = source.read(member)
    elif spec["kind"] == "tar.gz":
        with tarfile.open(archive) as source:
            member = next(
                m for m in source.getmembers() if Path(m.name).name == filename
            )
            data = source.extractfile(member).read()
    else:
        data = archive.read_bytes()
    # Keyed by the pin, so checkouts on other pins never swap the binary under
    # a running check (same pattern as the mod's old toolchain).
    key = _pin_key(spec)
    binary = cache_dir() / "bin" / key / filename
    if not binary.is_file() or binary.read_bytes() != data:
        replace(binary, data, mode=0o755)
    binary.chmod(0o755)
    return binary


def canonical_python():
    if sys.version_info < (3, 12):
        raise RuntimeError("Install native Python 3.12 or later to run project tools")
    if sys.version_info[:3] == (3, 12, 14) and (
        zlib.ZLIB_VERSION,
        zlib.ZLIB_RUNTIME_VERSION,
    ) == ("1.3.2", "1.3.2"):
        return Path(sys.executable)
    key = "-".join(host())
    version = lock_tool("python")["version"].split("+")[0]
    directory = cache_dir() / ("python-" + version + "-" + key)
    relative = Path("python") / ("python.exe" if os.name == "nt" else "bin/python3")
    python = directory / relative
    if not python.is_file():
        archive = fetch("python", "python-" + version + "-" + key + ".tar.gz")
        with (
            progress("Unpacking and verifying Python runtime"),
            tempfile.TemporaryDirectory(dir=cache_dir()) as temporary,
        ):
            staging = Path(temporary) / "install"
            staging.mkdir()
            with tarfile.open(archive) as source:
                source.extractall(staging, filter="data")
            probed = python_version(staging / relative)
            if probed != CANONICAL_PYTHON_PROBE:
                raise RuntimeError(f"Canonical Python compressor mismatch: {probed}")
            try:
                staging.rename(directory)
            except OSError:
                # Another process installed it first: use the winner when it
                # is a valid install (same checks as the cache hit), else raise.
                if (
                    not python.is_file()
                    or python_version(python) != CANONICAL_PYTHON_PROBE
                ):
                    raise
    probed = python_version(python)
    if probed != CANONICAL_PYTHON_PROBE:
        raise RuntimeError(f"Canonical Python compressor mismatch: {probed}")
    return python


def python_version(python):
    return call(
        python,
        "-I",
        "-X",
        "utf8",
        "-c",
        "import sys,zlib; print(sys.version_info[:3],zlib.ZLIB_VERSION,zlib.ZLIB_RUNTIME_VERSION)",
        capture=True,
    ).strip()


def prefetch(names):
    """Download named tools into the shared cache for CI: one explicit list,
    no lazy downloads inside later steps."""
    for name in names:
        tool(name)


def main(argv):
    # Only a leading flag selects prefetch; `toolchain.py script.py --ci`
    # must run the script. Prefetch takes nothing but the known flags: any
    # other argument is a caller mistake that must fail loudly (exit 2)
    # instead of exiting 0 without running anything.
    if argv and (argv[0] in ("--ci", "--tools") or argv[0].startswith("--tools=")):
        names = []
        index = 0
        while index < len(argv):
            arg = argv[index]
            if arg == "--ci":
                index += 1
            elif arg == "--tools" and index + 1 < len(argv):
                names.extend(name for name in argv[index + 1].split(",") if name)
                index += 2
            elif arg.startswith("--tools="):
                names.extend(name for name in arg.partition("=")[2].split(",") if name)
                index += 1
            else:
                print(
                    "usage: toolchain.py [--ci] [--tools name[,name]]",
                    file=sys.stderr,
                )
                return 2
        if "--ci" in argv and not names:
            names = ["just", "shellcheck"]
        prefetch(names)
        if "--ci" in argv:
            python = canonical_python()
            if "GITHUB_PATH" in os.environ:
                with Path(os.environ["GITHUB_PATH"]).open(
                    "a", encoding="utf-8", newline="\n"
                ) as output:
                    output.write("\n".join([*pinned_bins(), str(python.parent)]) + "\n")
    else:
        call(canonical_python(), "-I", "-X", "utf8", *argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


try:
    from toolchain_local import (  # noqa: F401
        JAVA_ASSETS,
        JAVA_VERSION,
        check_java,
        deb_libraries,
        deb_payload,
        java_home,
    )
except ImportError:
    # Standalone core without the workspace module (other repos vendor their
    # own repo-specific module next to their copy of the core).
    JAVA_VERSION = None
    JAVA_ASSETS = {}

    def check_java(*args, **kwargs):
        raise RuntimeError("toolchain_local is not vendored in this repo")

    def deb_payload(*args, **kwargs):
        raise RuntimeError("toolchain_local is not vendored in this repo")

    def deb_libraries(*args, **kwargs):
        raise RuntimeError("toolchain_local is not vendored in this repo")

    def java_home(*args, **kwargs):
        raise RuntimeError("toolchain_local is not vendored in this repo")
