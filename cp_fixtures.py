"""Test helpers: apply Workshop operations the way the CP stream delivers them."""

from uuid import uuid4

MAP_OPS = frozenset(
    {
        "map-create",
        "map-properties",
        "limbo-export",
        "commit",
        "compare",
        "snapshots",
        "snapshot-create",
        "snapshot-delete",
        "snapshot-copy",
        "checkout-fresh",
        "candidate",
        "rollback",
        "publish",
        "reject",
        "export",
    }
)


class Reporter:
    """In-memory CP state: the documents the controller read or reported."""

    def __init__(self):
        self.docs = {}

    def put(self, name, value):
        self.docs[name] = value

    def get(self, names=None):
        selected = self.docs if names is None else names
        return {
            name: dict(self.docs[name])
            if isinstance(self.docs[name], dict)
            else self.docs[name]
            for name in selected
            if name in self.docs
        }

    def doc(self, name, *, refresh=False):
        if name not in self.docs:
            return None
        value = self.docs[name]
        return dict(value) if isinstance(value, dict) else value


class FakeGameClient:
    """In-memory WorkshopGameClient double for controller/backend tests."""

    def __init__(self, responses=None, *, handler=None):
        self.calls = []
        self.responses = list(responses or [])
        self.handler = handler

    def command(self, game_op, *, job=None, expires_in_s=60, idempotency_key=None):
        self.calls.append((game_op, job, expires_in_s, idempotency_key))
        if self.responses:
            reply = self.responses.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        if self.handler is not None:
            return self.handler(
                game_op,
                job=job,
                expires_in_s=expires_in_s,
                idempotency_key=idempotency_key,
            )
        return {"state": "effective", "code": "ok", "output": "ok"}


def payload(kind, actor, **fields):
    """The CP payload for a former inbox kind (core.workshop_op shapes)."""
    actor = {"draslId": actor["draslId"], "name": actor["name"]}
    if kind == "members":
        return {"kind": "members", "target": "*", "grantor": actor, **fields}
    if kind in ("ai-key", "ai-gateway-key"):
        provider = "openrouter" if kind == "ai-key" else "vercel-gateway"
        return {
            "kind": "ai-key",
            "target": "*",
            "actor": actor,
            "provider": provider,
            **fields,
        }
    if kind == "ai-provider-key":
        return {"kind": "ai-key", "target": "*", "actor": actor, **fields}
    if kind in MAP_OPS:
        return {
            "kind": "map",
            "target": "*",
            "actor": actor,
            "mapOp": kind,
            "args": fields,
        }
    return {"kind": kind, "target": "*", "actor": actor, **fields}


def apply(controller, kind, actor, *, operation_id=None, **fields):
    """Apply one operation like cp_client.sync and return its result document."""
    if controller.state_reporter is None:
        controller.state_reporter = Reporter()
    operation_id = operation_id or str(uuid4())
    try:
        outcome = controller.apply_cp_operation(
            operation_id, payload(kind, actor, **fields), controller.members()
        )
    except BlockingIOError:
        raise
    except Exception as error:
        controller.cp_result(operation_id, error=error)
    else:
        controller.cp_result(operation_id, outcome)
    controller.flush_reports()
    return controller.state_reporter.docs[f"result:{operation_id}"]
