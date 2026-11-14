#!/usr/bin/env python3
"""Container entry point for the Workshop controller (stdlib only).

Own event loop: a reconcile step every BF_STEP_INTERVAL_S seconds (default
15), following a transition in flight with the same bounded budget as
``workshopctl step``. Workshop operations arrive over the control-plane
agent stream (a thread holds the SSE connection open) and wake the loop at
once; controller documents go back to CP after each pass. No systemd unit,
Docker schedule or helper runs this loop.

Health: GET /healthz on loopback BF_HEALTH_PORT (default 8080) reports the
controller's own state and loop tick. A sleeping or absent Game never fails
it: only an unreadable controller state degrades the endpoint.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
import signal
import sys
from pathlib import Path
import threading
import time

# Container runs under -I (no implicit cwd on sys.path): resolve siblings
# from the entry point's own directory, like scripts/toolchain.py does.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import workshopctl  # noqa: E402


def _positive_float(env, name, default):
    """Seconds-valued step tuning; garbage or a non-positive value fails fast."""
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number of seconds")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return value


def _non_negative_float(env, name, default):
    """Follow window; 0 disables following, garbage or negative fails fast."""
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number of seconds")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative number of seconds")
    return value


def make_handler(controller_getter, tick_getter):
    class HealthHandler(BaseHTTPRequestHandler):
        server_version = "WorkshopController/1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/healthz":
                self.send_response(404)
                self.end_headers()
                return
            try:
                state = controller_getter().status()
            except Exception as error:
                body = {"status": "degraded", "error": type(error).__name__}
                raw = json.dumps(body).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            body = {
                "status": "ok",
                "phase": state.get("phase"),
                "tick": tick_getter(),
            }
            raw = json.dumps(body, sort_keys=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return HealthHandler


def run_loop(controller, stop, env=os.environ):
    """One reconcile pass, then bounded following; returns when stop is set.

    Waits between passes on the CP wake event (a pushed frame), the step
    interval, or stop, whichever comes first.
    """
    interval = _positive_float(env, "BF_STEP_INTERVAL_S", 15)
    follow = _non_negative_float(env, "BF_STEP_FOLLOW_S", 12)
    poll = _positive_float(env, "BF_STEP_POLL_S", 2)
    cp = getattr(controller, "cp_client", None)
    wake = getattr(cp, "wake", None) or stop
    while not stop.is_set():
        tick = time.time()
        run_loop.tick = tick
        if wake is not stop:
            wake.clear()
        try:
            try:
                if cp is not None:
                    cp.sync(controller)
            except Exception as error:  # cp must never block reconciliation
                print(f"entrypoint: cp pass failed: {error!r}", file=sys.stderr)
            renewer = getattr(controller, "cp_renewer", None)
            if renewer is not None:
                renewer.tick()  # never raises: each key logs and retries
            result = controller.step()
            deadline = time.monotonic() + follow
            while (
                result.get("phase") in workshopctl.FOLLOWED_PHASES
                and time.monotonic() < deadline
                and not stop.is_set()
            ):
                time.sleep(poll)
                result = controller.step()
        except Exception as error:
            print(f"entrypoint: step failed: {error!r}", file=sys.stderr)
        flush = getattr(controller, "flush_reports", None)
        if callable(flush):
            flush()  # never raises: undelivered documents stay staged
        wake.wait(interval)


run_loop.tick = 0.0


def main(env=os.environ) -> int:
    backend_name = env.get("BF_WORKSHOP_BACKEND", "dokploy")
    controller, _backend = workshopctl.build_controller(
        env, workshopctl.backend_factory_from_name(backend_name, env)
    )
    stop = threading.Event()
    cp = getattr(controller, "cp_client", None)

    def shutdown(_signal, _frame):
        stop.set()
        if cp is not None:
            cp.wake.set()

    for name in ("SIGTERM", "SIGINT"):
        try:
            signal.signal(getattr(signal, name), shutdown)
        except (AttributeError, OSError, ValueError):
            pass
    try:
        # Initial catalog for CP readers; afterwards map operations re-export.
        controller.export_maps()
    except Exception as error:
        print(f"entrypoint: map export failed: {error!r}", file=sys.stderr)
    if cp is not None:
        threading.Thread(target=cp.run_stream, args=(stop,), daemon=True).start()
    state_reporter = getattr(controller, "state_reporter", None)
    if state_reporter is not None and callable(
        getattr(state_reporter, "run_state_stream", None)
    ):
        threading.Thread(
            target=state_reporter.run_state_stream,
            args=(stop, cp.wake if cp is not None else None),
            daemon=True,
        ).start()
    port = int(env.get("BF_HEALTH_PORT", 8080))
    server = HTTPServer(
        ("127.0.0.1", port), make_handler(lambda: controller, lambda: run_loop.tick)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    run_loop(controller, stop, env)
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
