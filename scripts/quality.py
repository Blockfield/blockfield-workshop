"""Run the repository's pinned checks with argument lists on every supported OS."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from toolchain import canonical_python, child_env, download, native, python_args, tool  # noqa: E402


def _git(root, *args):
    """git bound to this checkout's git dir.

    An invalid `.git` entry makes git fail instead of silently resolving to
    an outer repository (whose config we must never touch).
    """
    return ["git", f"--git-dir={root / '.git'}", *args]


def wire_hooks(root=ROOT, strict=False):
    """Point this checkout at the versioned hooks in `.githooks`.

    Wires only when git would run no other hooks here: core.hooksPath unset at
    every scope and no active hook in the hooks dir. Otherwise warns and leaves
    git alone. Returns True when `.githooks` is the hooks path afterwards.
    With strict, failing to read or write git config exits: `just setup`
    must not pass without the hooks (pre-push runs the secret scan).
    Notices go to stderr: callers capture the stdout of `quality.py run`.
    """
    # No `.githooks` (an older branch): nothing versioned to wire, so even
    # strict setup has nothing to enforce here.
    if not (root / ".git").exists() or not (root / ".githooks").is_dir():
        return False

    def git(*args, check=False):
        return subprocess.run(
            _git(root, *args),
            cwd=root,
            env=child_env(),
            stdout=subprocess.PIPE,
            text=True,
            errors="replace",  # a non-UTF-8 hooksPath must not crash every just call
            check=check,
        )

    try:
        current = git("config", "--get", "core.hooksPath").stdout.strip()
        if current == ".githooks":
            return True
        if current:
            print(
                f"warning: keeping core.hooksPath {current!r}; "
                "versioned .githooks not wired",
                file=sys.stderr,
            )
            return False
        hooks = (
            root / git("rev-parse", "--git-path", "hooks", check=True).stdout.strip()
        )
        active = (
            sorted(p.name for p in hooks.iterdir() if not p.name.endswith(".sample"))
            if hooks.is_dir()
            else []
        )
        if active:
            print(
                f"warning: keeping active hooks {', '.join(active)} in {hooks}; "
                "versioned .githooks not wired",
                file=sys.stderr,
            )
            return False
        git("config", "--local", "core.hooksPath", ".githooks", check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        if strict:
            raise SystemExit(
                f"just setup: cannot wire versioned hooks in {root}: {error}"
            )
        print(
            f"warning: cannot wire versioned hooks in {root}: {error}", file=sys.stderr
        )
        return False
    print(
        "commits and pushes now run the versioned .githooks (bypass: --no-verify)",
        file=sys.stderr,
    )
    return True


def run(*args, capture=False, input=None, cwd=ROOT):
    name = str(args[0])
    if name in (
        "ruff",
        "shellcheck",
        "shfmt",
        "actionlint",
        "prettier",
        "pnpm",
        "just",
    ):
        executable = str(tool(name))
    elif name == "python":
        executable = str(canonical_python())
    else:
        executable = native(name)
    command = [
        executable,
        *(python_args(args[1:]) if name == "python" else map(str, args[1:])),
    ]
    if name == "actionlint":
        with subprocess.Popen(command, cwd=cwd, env=child_env()) as process:
            print(
                f"actionlint PID {process.pid}: {executable}",
                file=sys.stderr,
                flush=True,
            )
            try:
                code = process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise
            if code:
                raise subprocess.CalledProcessError(code, command)
        return
    result = subprocess.run(
        command,
        cwd=cwd,
        env=child_env(),
        check=True,
        input=input,
        stdout=subprocess.PIPE if capture else None,
        text=True,
        shell=os.name == "nt" and Path(executable).suffix.lower() in (".bat", ".cmd"),
    )
    return result.stdout


def sources():
    tracked = run(
        "git",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        capture=True,
    )
    return sorted(
        {name for name in tracked.split("\0") if name and (ROOT / name).is_file()}
    )


def batches(files):
    batch, length = [], 0
    for name in files:
        size = len(name.encode("utf-16-le")) // 2 + 3
        if batch and length + size > 5000:
            yield batch
            batch, length = [], 0
        batch.append(name)
        length += size
    if batch:
        yield batch


def each(command, files, *, capture=False):
    for batch in batches(files):
        output = run(*command, *batch, capture=capture)
        if capture and output.strip():
            raise RuntimeError(output)


def eol():
    output = run("git", "ls-files", "--eol", capture=True)
    bad = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        index, _worktree, attr, path = fields
        if "attr/-text" in attr:
            continue  # stored byte-for-byte, Git never converts
        if index in ("i/crlf", "i/mixed") or path.lower().endswith((".bat", ".cmd")):
            bad.append(line.split("\t", 1)[1])
    if bad:
        raise SystemExit(
            "CRLF in the Git index or .bat/.cmd without -text:\n"
            + "\n".join(sorted(bad))
            + "\nFix with: git add --renormalize ."
        )


def kotlin(files, format=False):
    if not files:
        return
    jar = download(
        "ktlint-1.8.0.jar",
        "https://github.com/ktlint/ktlint/releases/download/1.8.0/ktlint",
        "a3fd620207d5c40da6ca789b95e7f823c54e854b7fade7f613e91096a3706d75",
    )
    run(
        "java",
        "-Xmx512m",
        "-jar",
        jar,
        "--relative",
        "--patterns-from-stdin=",
        *(["--format"] if format else []),
        input="\0".join(files) + "\0",
    )


def lint(files):
    run("actionlint")
    each(["ruff", "check"], [f for f in files if f.endswith(".py")])
    each(["shellcheck", "--severity=error"], [f for f in files if f.endswith(".sh")])
    kotlin([f for f in files if f.endswith((".kt", ".kts"))])
    if (ROOT / "go.mod").is_file():
        run("go", "vet", "-p", "1", "./...")
    if (ROOT / "Cargo.toml").is_file():
        run("cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings")
    if (ROOT / "package.json").is_file():
        import json

        scripts = json.loads((ROOT / "package.json").read_text())["scripts"]
        manager = "pnpm" if (ROOT / "pnpm-lock.yaml").is_file() else "npm"
        for name in ("lint", "stylelint", "typecheck"):
            if name in scripts:
                run(manager, "run", name)


def formatting(files, write=False):
    eol()
    each(
        ["ruff", "format", *([] if write else ["--check"])],
        [f for f in files if f.endswith(".py")],
    )
    java = [f for f in files if f.endswith(".java")]
    if java:
        jar = download(
            "google-java-format-1.36.1-all-deps.jar",
            "https://repo.maven.apache.org/maven2/com/google/googlejavaformat/google-java-format/1.36.1/google-java-format-1.36.1-all-deps.jar",
            "25b400f003089d23cc5320cdaf1a16cabee19b8aa3434d0ff021b3d9f42154b4",
        )
        each(
            [
                "java",
                "-jar",
                jar,
                "--aosp",
                *(["--replace"] if write else ["--dry-run", "--set-exit-if-changed"]),
            ],
            java,
        )
    each(
        ["shfmt", "-i", "4", "-ci", "-w" if write else "-d"],
        [f for f in files if f.endswith(".sh")],
    )
    kotlin([f for f in files if f.endswith((".kt", ".kts"))], format=write)
    if (ROOT / "go.mod").is_file():
        each(
            ["gofmt", "-w" if write else "-l"],
            [f for f in files if f.endswith(".go")],
            capture=not write,
        )
    if (ROOT / "Cargo.toml").is_file():
        run("cargo", "fmt", "--all", *([] if write else ["--", "--check"]))
    if (ROOT / "package.json").is_file():
        manager = "pnpm" if (ROOT / "pnpm-lock.yaml").is_file() else "npm"
        run(manager, "run", "format" if write else "format:check")
    docs = [
        f
        for f in files
        if f.endswith(".md")
        or (f.startswith(".github/") and f.endswith((".yml", ".yaml")))
    ]
    docs.extend(f for f in (".mcp.json", "pnpm-workspace.yaml") if (ROOT / f).is_file())
    each(["prettier", "--write" if write else "--check"], docs)
    if (ROOT / "justfile").is_file():
        run("just", "--fmt", *([] if write else ["--check"]))


def staged_files():
    """Staged, still-present worktree files, repo-relative (empty outside commits)."""
    try:
        staged = run(
            "git",
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--diff-filter=ACMR",
            "--",
            capture=True,
        )
    except subprocess.CalledProcessError:
        return []
    return sorted({n for n in staged.split("\0") if n and (ROOT / n).is_file()})


def format_staged():
    """Format the staged files with the same pinned formatters `format-check`
    uses, and re-stage them. Files with unstaged edits are skipped with a
    warning (formatting the worktree copy would stage unreviewed hunks), so
    only fully staged files are re-added. No Gradle.

    Per-file writers (prettier, ruff, GJF, ...) run only on staged files;
    repo-wide steps (npm format, cargo fmt, just --fmt) run only when a file
    in their scope is staged. Repo-wide steps may reformat other worktree
    files of the same type, but those are never staged by this hook.
    """
    files = staged_files()
    if not files:
        return
    # Fail closed: if the worktree diff cannot be read, the hook must stop
    # the commit rather than risk staging unreviewed hunks.
    worktree = run(
        "git",
        "diff",
        "--name-only",
        "-z",
        "--",
        capture=True,
    )
    dirty = {n for n in worktree.split("\0") if n} & set(files)
    if dirty:
        print(
            "pre-commit: skipping partially staged files "
            "(format them after staging, or unstage first):\n"
            + "\n".join(f"  {n}" for n in sorted(dirty))
        )
        files = [f for f in files if f not in dirty]
        if not files:
            return
    staged = set(files)
    eol()
    each(
        ["ruff", "format"],
        [f for f in files if f.endswith(".py")],
    )
    java = [f for f in files if f.endswith(".java")]
    if java:
        jar = download(
            "google-java-format-1.36.1-all-deps.jar",
            "https://repo.maven.apache.org/maven2/com/google/googlejavaformat/google-java-format/1.36.1/google-java-format-1.36.1-all-deps.jar",
            "25b400f003089d23cc5320cdaf1a16cabee19b8aa3434d0ff021b3d9f42154b4",
        )
        each(
            ["java", "-jar", jar, "--aosp", "--replace"],
            java,
        )
    each(
        ["shfmt", "-i", "4", "-ci", "-w"],
        [f for f in files if f.endswith(".sh")],
    )
    kotlin([f for f in files if f.endswith((".kt", ".kts"))], format=True)
    if (ROOT / "go.mod").is_file():
        each(
            ["gofmt", "-w"],
            [f for f in files if f.endswith(".go")],
        )
    if (ROOT / "Cargo.toml").is_file() and any(
        f.endswith((".rs", ".toml")) for f in files
    ):
        run("cargo", "fmt", "--all")
    if (ROOT / "package.json").is_file() and any(
        f.endswith((".js", ".mjs", ".ts", ".tsx", ".css", ".json")) for f in files
    ):
        import json

        if "format" in json.loads((ROOT / "package.json").read_text())["scripts"]:
            manager = "pnpm" if (ROOT / "pnpm-lock.yaml").is_file() else "npm"
            run(manager, "run", "format")
    docs = [
        f
        for f in files
        if f.endswith(".md")
        or (f.startswith(".github/") and f.endswith((".yml", ".yaml")))
    ]
    docs.extend(
        f
        for f in (".mcp.json", "pnpm-workspace.yaml")
        if f in staged and (ROOT / f).is_file()
    )
    each(["prettier", "--write"], docs)
    if (ROOT / "justfile").is_file() and "justfile" in staged:
        run("just", "--fmt")
    run("git", "add", "--", *files)


def gradle_command(wrapper, windows=None):
    windows = os.name == "nt" if windows is None else windows
    return wrapper.with_name("gradlew.bat") if windows else wrapper


def compile_check(files):
    wrappers = [ROOT / f for f in files if Path(f).name == "gradlew"]
    if wrappers:
        for wrapper in wrappers:
            run(
                gradle_command(wrapper),
                "check",
                "--no-daemon",
                "--max-workers=2",
                "-Dorg.gradle.jvmargs=-Xmx2G",
                cwd=wrapper.parent,
            )
    elif "scripts/PatchLrTactical.java" in files:
        jars = [
            download(
                "asm-9.8.jar",
                "https://repo.maven.apache.org/maven2/org/ow2/asm/asm/9.8/asm-9.8.jar",
                "876eab6a83daecad5ca67eb9fcabb063c97b5aeb8cf1fca7a989ecde17522051",
            ),
            download(
                "asm-tree-9.8.jar",
                "https://repo.maven.apache.org/maven2/org/ow2/asm/asm-tree/9.8/asm-tree-9.8.jar",
                "14b7880cb7c85eed101e2710432fc3ffb83275532a6a894dc4c4095d49ad59f1",
            ),
        ]
        with tempfile.TemporaryDirectory() as output:
            run(
                "javac",
                "-proc:none",
                "-Xlint:divzero,empty,fallthrough,finally,-removal",
                "-Werror",
                "-cp",
                os.pathsep.join(map(str, jars)),
                "-d",
                output,
                "scripts/PatchLrTactical.java",
            )


def tests():
    if (ROOT / "package.json").is_file():
        import json

        if "test" in json.loads((ROOT / "package.json").read_text())["scripts"]:
            run("pnpm" if (ROOT / "pnpm-lock.yaml").is_file() else "npm", "run", "test")
    for directory in (".",):
        if (ROOT / directory).is_dir():
            patterns = ["test_*.py"]
            if directory == "." and os.name == "nt":
                # The Docker controller requires Linux flock, /proc and dir_fd semantics.
                patterns = [
                    "test_map_package*.py",
                    "test_package_review.py",
                    "test_journal.py",
                    "test_toolchain.py",
                ]
                print(
                    "Windows: portable workshop checks; Docker controller suite runs on Linux."
                )
            for pattern in patterns:
                run(
                    "python",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    directory,
                    "-p",
                    pattern,
                    "-v",
                )
    if (ROOT / "go.mod").is_file():
        run("go", "test", "-p", "1", "./...")
    if (ROOT / "Cargo.toml").is_file():
        run("cargo", "test", "--workspace", "--locked")
    if (ROOT / "scripts/test-prepare-updater.py").is_file():
        run("python", "scripts/test-prepare-updater.py")


def main():
    python = canonical_python()
    if Path(sys.executable).resolve() != python.resolve():
        raise SystemExit(
            subprocess.run(
                [str(python), "-I", "-X", "utf8", __file__, *sys.argv[1:]],
                env=child_env(),
            ).returncode
        )
    mode = sys.argv[1]
    if mode == "run":
        run(*sys.argv[2:])
        return
    files = sources()
    # Every just entry point wires the versioned hooks first, so a fresh
    # clone gets them on first use even when `just setup` never ran.
    # Only sets core.hooksPath when it is unset; never fails the command.
    wire_hooks()

    if mode == "setup":
        wire_hooks(strict=True)
        if (ROOT / "package.json").is_file():
            if (ROOT / "pnpm-lock.yaml").is_file():
                run("pnpm", "install", "--frozen-lockfile")
            else:
                run("npm", "ci")
        for name in ("ruff", "actionlint", "prettier"):
            tool(name)
        if any(f.endswith(".sh") for f in files):
            tool("shellcheck")
            tool("shfmt")
        if "GITHUB_PATH" in os.environ and (ROOT / "pnpm-lock.yaml").is_file():
            with Path(os.environ["GITHUB_PATH"]).open(
                "a", encoding="utf-8", newline="\n"
            ) as output:
                output.write(str(ROOT / ".cache/quality/npm/node_modules/.bin") + "\n")
        return
    if mode in ("dev", "start", "test-postgres"):
        if (ROOT / "package.json").is_file():
            run(
                "pnpm" if (ROOT / "pnpm-lock.yaml").is_file() else "npm",
                "run",
                "test:postgres" if mode == "test-postgres" else mode,
                *sys.argv[2:],
            )
        elif (ROOT / "main.go").is_file() and mode in ("dev", "start"):
            run("go", "run", ".", *sys.argv[2:])
        else:
            raise SystemExit("No matching application command in this repository")
        return
    if mode == "build":
        wrappers = [ROOT / f for f in files if Path(f).name == "gradlew"]
        if wrappers:
            for wrapper in wrappers:
                run(
                    gradle_command(wrapper),
                    "build",
                    "--no-daemon",
                    "--max-workers=2",
                    "-Dorg.gradle.jvmargs=-Xmx2G",
                    cwd=wrapper.parent,
                )
        elif (ROOT / "package.json").is_file():
            run(
                "pnpm" if (ROOT / "pnpm-lock.yaml").is_file() else "npm", "run", "build"
            )
        elif (ROOT / "go.mod").is_file():
            run("go", "build", "-p", "1", "./...")
        elif (ROOT / "scripts/build.py").is_file():
            run("python", "scripts/build.py", *sys.argv[2:])
        elif (ROOT / "scripts/patch.py").is_file():
            run("python", "scripts/patch.py", *sys.argv[2:])
        elif (ROOT / "scripts/build-music.py").is_file():
            run("python", "scripts/build-music.py", *sys.argv[2:], "--no-publish")
        elif (ROOT / "build.py").is_file():
            run("python", "build.py", *sys.argv[2:])
        elif (ROOT / "scripts/build-client.py").is_file():
            run("python", "scripts/build-client.py", *sys.argv[2:])
        elif (ROOT / "scripts/build-distribution.py").is_file():
            run("python", "scripts/build-distribution.py", *sys.argv[2:])
        else:
            raise SystemExit("This repository has no standalone build target")
        return
    if mode in ("lint", "check"):
        lint(files)
    if mode in ("format-check", "check"):
        formatting(files)
    if mode == "format":
        formatting(files, write=True)
    if mode == "format-staged":
        format_staged()
        return
    if mode in ("test", "check"):
        tests()
    if mode in ("compile", "check"):
        compile_check(files)


if __name__ == "__main__":
    main()
