"""Protobuf shaping required by the robot's app bridge before a message goes on the Zenoh bus.

The bridge converts every `app/cmd/...` / `app/data/...` Protobuf into a fixed-layout struct; fixed-size arrays
and `proto3 optional` scalars must therefore be fully populated or the frame is silently dropped. This module also fills a message's `data` sub-message from plain keyword values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

# Fixed-length `bytes` fields (`[u8; N]` on the robot). Omitting them or sending fewer than N bytes makes the
# robot's bridge reject and drop the `app/cmd/...` Protobuf frame.
_FIXED_BYTES_FIELD_SIZES: dict[tuple[str, str], int] = {
    ("AnalyticsDeliveryId", "delivery_id"): 16,
    ("AudioPlayFileReqData", "audio_path"): 128,
    ("CommonGetPartitionSizeReqData", "name"): 32,
    ("CommonGetPartitionSizeReqData", "reserved"): 3,
    ("CommonPingReqData", "ping_data"): 16,
    ("DeviceSecurityDeviceActivateReqData", "request_id"): 16,
    ("DeviceSecurityDeviceAttestReqData", "challenge"): 32,
    ("DeviceSecurityDeviceDeactivateReqData", "request_id"): 16,
    ("DeviceSecurityE2eProvisionReqData", "challenge"): 32,
    ("DeviceSecurityE2eProvisionReqData", "request_id"): 16,
    ("HmsActionResponseData", "delivery_store_id"): 16,
    ("LedEffectCtrlData", "reserved"): 2,
    ("LogWithMetadataReqData", "buf"): 200,
    ("MdGatewayMqttEndpointUpdateReqData", "resv"): 3,
    ("MicSourceSelectReqData", "mac"): 6,
    ("MissionRequestData", "mode_params"): 9,
    ("NmWifiConnectReqData", "resv"): 4,
    ("NmWifiConnectReqData", "uuid"): 16,
    ("NmWifiProvisionCommitReqData", "resv"): 7,
    ("NmWifiProvisionCommitReqData", "uuid"): 16,
    ("NmWifiProvisionResultReqData", "resv"): 8,
    ("NmWifiProvisionResultReqData", "uuid"): 16,
    ("NmWifiProvisionStartReqData", "mqtt_resv"): 7,
    ("NmWifiProvisionStartReqData", "resv"): 7,
    ("NmWifiProvisionStartReqData", "uuid"): 16,
    ("NmWifiScanReqData", "resv"): 6,
    ("NmWifiSwitchToApReqData", "resv"): 7,
    ("NmWifiSwitchToStationReqData", "resv"): 6,
    ("NmWifiSwitchToStationResultReqData", "resv"): 8,
    ("ParameterQueryReqData", "reserved"): 2,
    ("ParameterUpdateReqData", "reserved"): 2,
    ("TestResultData", "reserved"): 2,
    ("TestResultData", "session_id"): 4,
    ("TrackingParamQueryReqData", "reserved"): 3,
    ("TrackingParamUpdateReqData", "reserved"): 3,
    ("VtStatusReqData", "resv"): 3,
}

# Exact fixed-size `repeated` scalar arrays (`[T; N]`) the robot's bridge requires:
_FIXED_REPEATED_FIELD_SIZES: dict[tuple[str, str], int] = {
    ("GamePlayerInputData", "acc_g"): 3,
    ("GamePlayerInputData", "attitude_deg"): 3,
    ("GamePlayerInputData", "gyro_dps"): 3,
    ("LedCtrlOnewayData", "rgbw_data"): 14,
    ("LedEffectCtrlData", "csv_path"): 128,
}


def normalize_proto_for_videotrans(sub_msg: Message) -> None:
    """Populate `proto3 optional` scalar defaults and fixed `[T; N]` slices expected by the robot's app bridge."""
    msg_name = sub_msg.DESCRIPTOR.name
    for fdesc in sub_msg.DESCRIPTOR.fields:
        if fdesc.is_repeated:
            if fdesc.message_type is not None:
                for item in getattr(sub_msg, fdesc.name):
                    normalize_proto_for_videotrans(item)
            else:
                fixed_rep = _FIXED_REPEATED_FIELD_SIZES.get((msg_name, fdesc.name))
                if fixed_rep is not None:
                    rep_field = getattr(sub_msg, fdesc.name)
                    cur_vals = list(rep_field)
                    if len(cur_vals) != fixed_rep:
                        pad_val = 0.0 if fdesc.type in (FieldDescriptor.TYPE_DOUBLE, FieldDescriptor.TYPE_FLOAT) else 0
                        norm_vals = (cur_vals + [pad_val] * fixed_rep)[:fixed_rep]
                        del rep_field[:]
                        rep_field.extend(norm_vals)
            continue
        if fdesc.message_type is not None:
            if sub_msg.HasField(fdesc.name):
                normalize_proto_for_videotrans(getattr(sub_msg, fdesc.name))
            continue
        if fdesc.type == FieldDescriptor.TYPE_BYTES:
            fixed_len = _FIXED_BYTES_FIELD_SIZES.get((msg_name, fdesc.name))
            if fixed_len is not None:
                cur = bytes(getattr(sub_msg, fdesc.name))
                if len(cur) != fixed_len:
                    setattr(sub_msg, fdesc.name, cur.ljust(fixed_len, b"\x00")[:fixed_len])
            continue
        if fdesc.has_presence and not sub_msg.HasField(fdesc.name):
            # Explicitly set the default scalar so the robot sees the field as present (0), not missing
            setattr(sub_msg, fdesc.name, getattr(sub_msg, fdesc.name))


_normalize_proto_for_videotrans = normalize_proto_for_videotrans  # backwards-compatible alias (tests import it)


def _coerce_scalar(fdesc: FieldDescriptor | None, value: Any) -> Any:
    """str -> bytes for `bytes` fields and bytes -> str for `string` fields (lenient caller input)."""
    if fdesc is None:
        return value
    if fdesc.type == FieldDescriptor.TYPE_BYTES and isinstance(value, str):
        return value.encode("utf-8")
    if fdesc.type == FieldDescriptor.TYPE_STRING and isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def fill_message_data(msg: Message, data_fields: Mapping[str, Any]) -> None:
    """Populate `msg.data` from keyword values; `None` values are skipped.

    Lists extend repeated fields, `Message` values are copied, plain dicts fill a nested message field by field
    (whole-field assignment is forbidden by protobuf), everything else is assigned directly.
    """
    if not hasattr(msg, "data"):
        return
    data = msg.data
    data_desc = data.DESCRIPTOR.fields_by_name
    for key, raw_value in data_fields.items():
        if raw_value is None:
            continue
        fdesc = data_desc.get(key)
        value = raw_value if fdesc is None or fdesc.is_repeated else _coerce_scalar(fdesc, raw_value)
        attr = getattr(data, key)
        if hasattr(attr, "extend") and isinstance(value, (list, tuple)):
            attr.extend(value)
        elif hasattr(attr, "CopyFrom") and isinstance(value, Message):
            attr.CopyFrom(value)
        elif hasattr(attr, "DESCRIPTOR") and isinstance(value, dict):
            sub_desc = attr.DESCRIPTOR.fields_by_name
            for sub_key, sub_value in value.items():
                if sub_value is None:
                    continue
                sub_fdesc = sub_desc.get(sub_key)
                if sub_fdesc is None:
                    continue
                setattr(attr, sub_key, _coerce_scalar(sub_fdesc, sub_value))
        else:
            setattr(data, key, value)
