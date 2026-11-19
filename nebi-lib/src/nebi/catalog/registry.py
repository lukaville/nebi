"""`CATALOG`: one place to look up topics (and their Protobuf classes), camera profiles, parameters and faults.

The tables themselves live in `topics.py`, `camera_profiles.py`, `params.py` and `faults.py`.
"""

from __future__ import annotations

import importlib
from typing import Any

from google.protobuf.message import Message

from nebi.catalog.camera_profiles import CAMERA_PROFILES, CameraProfileSpec
from nebi.catalog.faults import FAULTS, fault_summary
from nebi.catalog.params import PARAMS, ParamSpec
from nebi.catalog.topics import TOPICS, TopicSpec

__all__ = ["CATALOG", "CameraProfileSpec", "NebiCatalog", "ParamSpec", "TopicSpec"]

_PROTO_PKG = "nebi.proto.md.protocol"


def normalize_topic(topic: str) -> str:
    """`app/cmd/x`, `/device/cmd/x`, `cmd/x` -> `/cmd/x`."""
    norm = "/" + topic.lstrip("/")
    for prefix in ("/app/", "/device/"):
        if norm.startswith(prefix):
            norm = "/" + norm[len(prefix) :]
    return norm


class NebiCatalog:
    """Read-only lookups over the catalog tables."""

    def __init__(self) -> None:
        self.topics: dict[str, TopicSpec] = {t.path: t for t in TOPICS}
        self.camera_profiles: list[CameraProfileSpec] = list(CAMERA_PROFILES)
        self.params: list[ParamSpec] = list(PARAMS)
        self.fault_codes: frozenset[int] = frozenset(FAULTS)
        self._cls_cache: dict[str, type[Message] | None] = {}

    def param(self, key: str) -> ParamSpec | None:
        return next((p for p in self.params if p.key == key), None)

    def param_by_id(self, param_id: int) -> ParamSpec | None:
        return next((p for p in self.params if p.param_id == param_id), None)

    def get_proto_class(self, topic_name: str) -> type[Message] | None:
        """Protobuf message class for a topic (any `app/` / `device/` prefix is ignored); None if unknown."""
        norm = normalize_topic(topic_name)
        if norm not in self._cls_cache:
            spec = self.topics.get(norm)
            cls = None
            if spec is not None:
                cls = getattr(importlib.import_module(f"{_PROTO_PKG}.{spec.module}"), spec.message)
            self._cls_cache[norm] = cls
        return self._cls_cache[norm]

    def decode_hms(self, code: int | str, instance: int = 0) -> dict[str, Any]:
        """Describe a health fault: code (`0x...`), instance, severity, English message and recovery kind."""
        if isinstance(code, str):
            s = code.strip()
            code = int(s[2:] if s.lower().startswith("0x") else s, 16)
        code &= 0xFFFFFFFF
        severity, recovery, message = fault_summary(code, int(instance))
        return {
            "code": f"0x{code:08X}",
            "instance": int(instance),
            "severity": severity,
            "message_en": message,
            "recovery_kind": recovery,
        }


CATALOG = NebiCatalog()
