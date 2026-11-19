"""System and raw-protocol commands: params, remote, topics, pub, req."""

from __future__ import annotations

import json
from typing import Any

from google.protobuf.json_format import MessageToDict

from nebi.catalog.registry import CATALOG
from nebi_cli import spec as S
from nebi_cli.errors import AgentError, bad_arg
from nebi_cli.registry import Args, CommandGroup, command
from nebi_cli.util import param_values


def parse_fields(raw: Any) -> dict[str, Any]:
    """`fields` argument of pub/req: a JSON object (or its text)."""
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise bad_arg(f"fields is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise AgentError("BAD_ARGUMENT", "fields must be a JSON object.", exit_code=S.EXIT_USAGE)
    return raw


def _unknown_topic(exc: KeyError) -> AgentError:
    return AgentError("UNKNOWN_TOPIC", str(exc), hint="`nebi topics <search>` lists valid topics.", exit_code=S.EXIT_USAGE)


class SystemCommands(CommandGroup):
    @command("params")
    async def params(self, a: Args) -> dict[str, Any]:
        c = self.client()
        if a.get("set") is not None:
            vals = a["set"]
            if not isinstance(vals, dict):
                raise bad_arg("--set must be a JSON object.")
            res = await c.system.set_parameters(vals)
            self.motion.forget_kinematics(c)
            return {"set": vals, "response": res}
        keys = [k.strip() for k in (a.get("keys") or "").split(",") if k.strip()] or None
        return {"params": param_values(await c.system.get_parameters(keys))}

    @command("remote")
    async def remote(self, a: Args) -> dict[str, Any]:
        c = self.client()
        return await c.system.vibrate_remote(slot=int(a.get("slot") or 0), level=int(a.get("level") or 2))

    @command("topics")
    async def topics(self, a: Args) -> dict[str, Any]:
        q = (a.get("search") or "").lower()
        items = [
            {"topic": t.path, "type": t.message, "app_can_publish": t.app_sends, "app_receives": t.app_receives}
            for t in CATALOG.topics.values()
            if not q or q in t.path.lower() or q in t.message.lower()
        ]
        return {"count": len(items), "topics": items}

    @command("pub")
    async def pub(self, a: Args) -> dict[str, Any]:
        c = self.client()
        try:
            seq = c.bus.publish(a["topic"], **parse_fields(a.get("fields")))
        except KeyError as exc:
            raise _unknown_topic(exc) from exc
        return {"topic": a["topic"], "seq": seq}

    @command("req")
    async def req(self, a: Args) -> dict[str, Any]:
        c = self.client()
        try:
            rsp = await c.bus.request(
                a["topic"], a.get("response") or None, timeout=float(a.get("timeout") or 3.0), **parse_fields(a.get("fields"))
            )
        except KeyError as exc:
            raise _unknown_topic(exc) from exc
        return {"topic": a["topic"], "response": MessageToDict(rsp, preserving_proto_field_name=True)}
