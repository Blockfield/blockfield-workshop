"""Terminal progress on stderr; stdout stays available for JSON and MCP."""

import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager


@contextmanager
def progress(message, *, animate=True):
    stream = sys.stderr
    interactive = animate and stream.isatty() and os.environ.get("TERM") != "dumb"
    color = (
        stream.isatty()
        and os.environ.get("TERM") != "dumb"
        and "NO_COLOR" not in os.environ
    )
    unicode = "utf" in (getattr(stream, "encoding", "") or "").lower()
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if unicode else "|/-\\"
    started = time.monotonic()
    stopped = threading.Event()
    width = 0

    def render(mark, *, final=False, code="36"):
        nonlocal width
        elapsed = time.monotonic() - started
        timer = f"{elapsed:.1f}s" if final else f"{int(elapsed)}s"
        title = message
        if interactive:
            columns = max(4, shutil.get_terminal_size((80, 24)).columns - 1)
            title = title[: max(0, columns - len(mark) - len(timer) - 3)]
        label = f"{title}  {timer}"
        text = f"{mark} {label}"
        padding = " " * max(0, width - len(text)) if interactive else ""
        width = len(text)
        if color:
            text = f"\033[{code}m{mark}\033[0m {label}"
        stream.write(
            ("\r" if interactive else "")
            + text
            + padding
            + ("\n" if final or not interactive else "")
        )
        stream.flush()

    def tick():
        index = 0
        while not stopped.wait(0.1 if interactive else 10):
            index += 1
            render(frames[index % len(frames)] if interactive else "...")

    render(frames[0] if interactive else ">")
    worker = threading.Thread(target=tick, name="cli-progress", daemon=True)
    worker.start()
    mark, code = ("✓" if unicode else "ok"), "32"
    try:
        yield
    except BaseException as error:
        mark = (
            "!" if isinstance(error, KeyboardInterrupt) else ("✗" if unicode else "x")
        )
        code = "31"
        raise
    finally:
        stopped.set()
        worker.join(timeout=1)
        render(mark, final=True, code=code)
