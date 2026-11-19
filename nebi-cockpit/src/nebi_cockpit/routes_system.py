"""Catalog, audio/intercom, parameters, LEDs, UWB, HMS, robot Wi-Fi and OTA routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile

from nebi.catalog.registry import CATALOG
from nebi.client import AsyncNebiClient
from nebi.domains.audio import SOUND_EFFECT_CATALOG
from nebi.exceptions import NebiRpcTimeoutError
from nebi.types import SOFTAP_HOST, USB_NCM_HOST
from nebi_cockpit.deps import ClientDep, ManagerDep, SimulatorDep
from nebi_cockpit.schemas import (
    HmsCodeBody,
    IntValueBody,
    LedExpressionBody,
    LedRgbBody,
    MicSelectBody,
    ParamsUpdateBody,
    PlayFileBody,
    ToggleBody,
    UwbVibrateBody,
    WifiConnectBody,
    WifiProvisionBody,
)

# Connection menu, in the order shown. `id` is what POST /api/connection (or /api/ble/provision_home_wifi for
# `ble_home_wifi`) takes; every older flow id (auto, usb_ncm, ...) is still accepted by the API.
FLOWS = [
    {
        "id": "lan_sta",
        "name": "Home Wi-Fi",
        "desc": "Nebi is already on your Wi-Fi network. Leave the IP blank to search for it.",
        "badge": "Usual",
    },
    {
        "id": "ble_home_wifi",
        "name": "Wake up over Bluetooth and connect to Home Wi-Fi",
        "desc": "After every power-on Nebi only listens on Bluetooth: pair, send it your Wi-Fi, then connect.",
        "badge": "After power-on",
    },
    {
        "id": "softap",
        "name": "Nebi's hotspot",
        "desc": f"This computer is joined to Nebi's own Wi-Fi hotspot ({SOFTAP_HOST}).",
    },
    {
        "id": "ble_bootstrap",
        "name": "Hotspot via Bluetooth",
        "desc": "Pair over Bluetooth to get the hotspot name and password, then connect through the hotspot.",
    },
    {
        "id": "usb_ncm",
        "name": "USB-C cable",
        "desc": f"Nebi is plugged into this computer with a USB-C data cable and shows up as a network adapter ({USB_NCM_HOST}).",
    },
    {"id": "simulator", "name": "Simulator", "desc": "Built-in virtual robot for testing; no hardware needed."},
]
DEFAULT_FLOW = "lan_sta"
UNSUPPORTED_FLOWS: list[dict[str, str]] = []  # flows the menu lists as unavailable (with a reason); none today
GAMES = [
    {"id": 1, "key": "motion_sense", "name": "Motion Sense Racing"},
    {"id": 2, "key": "tug_of_war", "name": "Robotic Tug-of-War"},
    {"id": 3, "key": "camera_battle", "name": "Camera Tag Battle"},
    {"id": 4, "key": "balance", "name": "Balance Challenge"},
    {"id": 5, "key": "treasure_hunt", "name": "AR Treasure Hunt"},
]
LED_PRESETS = ["happy.csv", "wink.csv", "heart.csv", "cool.csv", "sleepy.csv", "dizzy.csv", "angry.csv", "music_pulse.csv"]


def _led_result(client: AsyncNebiClient, seq: int) -> dict[str, Any]:
    # `led_direct_supported` kept for older clients (always true); the richer API is /api/eyes (routes_eyes.py)
    return {"ok": True, "seq": seq, "led_direct_supported": bool(client.telemetry.led_direct_supported),
            "eyes": client.system.eyes_status()}


router = APIRouter(prefix="/api", tags=["system"])


@router.get("/catalog")
async def api_catalog() -> dict[str, Any]:
    return {
        "topic_count": len(CATALOG.topics),
        "camera_profiles": [{**asdict(p), "label": p.label} for p in CATALOG.camera_profiles],
        "params": [{**asdict(p), "group": p.group} for p in CATALOG.params],
        "flows": FLOWS,
        "default_flow": DEFAULT_FLOW,
        "unsupported_flows": UNSUPPORTED_FLOWS,
        "games": GAMES,
        "led_presets": LED_PRESETS,
        "sound_effects": [{"id": s["id"], "name": s["name"]} for s in SOUND_EFFECT_CATALOG],
    }


# --- Audio, voice, intercom & wireless mics ---


@router.post("/audio/sound_effect")
async def api_sfx(body: IntValueBody, client: ClientDep) -> dict[str, Any]:
    return await client.audio.play_sound_effect(body.value)


@router.post("/audio/play_file")
async def api_play_file(body: PlayFileBody, client: ClientDep) -> dict[str, Any]:
    return await client.audio.play_audio_file(body.audio_path, body.play_action)


@router.post("/audio/voice_wakeup")
async def api_voice_wakeup(body: ToggleBody, client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.audio.set_voice_wakeup(body.enabled)}


@router.post("/audio/intercom")
async def api_audio_intercom(body: ToggleBody, client: ClientDep) -> dict[str, Any]:
    ic = client.intercom
    return await (ic.start() if body.enabled else ic.stop())


@router.get("/audio/intercom")
async def api_audio_intercom_status(client: ClientDep) -> dict[str, Any]:
    return client.intercom.status()


@router.get("/audio/parameter")
async def api_audio_parameter(category: int, parameter_id: int, client: ClientDep) -> dict[str, Any]:
    """Diagnostic: raw audio-service parameter read (`/cmd/common/get_parameter_req`)."""
    return await client.audio.get_parameter(category, parameter_id)


@router.post("/audio/mic/scan")
async def api_mic_scan(body: ToggleBody, client: ClientDep) -> dict[str, Any]:
    return await client.audio.scan_wireless_mics(start=body.enabled)


@router.post("/audio/mic/select")
async def api_mic_select(body: MicSelectBody, client: ClientDep) -> dict[str, Any]:
    return await client.audio.select_mic_source(body.kind, body.mac)


# --- System, parameters, LEDs, UWB, HMS ---


@router.post("/system/ping")
async def api_ping(client: ClientDep) -> dict[str, Any]:
    return await client.system.ping()


@router.get("/system/params")
async def api_get_params(client: ClientDep) -> dict[str, Any]:
    return await client.system.get_parameters()


@router.post("/system/params")
async def api_set_params(body: ParamsUpdateBody, client: ClientDep) -> dict[str, Any]:
    return await client.system.set_parameters(body.values)


@router.post("/system/led_expression")
async def api_led_expr(body: LedExpressionBody, client: ClientDep) -> dict[str, Any]:
    return _led_result(client, await client.system.set_led_expression(body.effect, body.play_count))


@router.post("/system/led_rgb")
async def api_led_rgb(body: LedRgbBody, client: ClientDep) -> dict[str, Any]:
    return _led_result(client, await client.system.set_led_rgbw(body.r, body.g, body.b))


@router.post("/system/uwb/vibrate")
async def api_uwb_vibrate(body: UwbVibrateBody, client: ClientDep) -> dict[str, Any]:
    return await client.system.vibrate_remote(body.slot, body.level)


@router.get("/system/hms")
async def api_hms(client: ClientDep) -> dict[str, Any]:
    """Active faults (from /status/hms/event_broadcast raise/clear events) and the recent event log."""
    t = client.telemetry
    return {"active": [asdict(a) for a in t.hms_alerts], "events": list(t.hms_events), "count": len(t.hms_alerts)}


@router.post("/system/hms/refresh")
async def api_hms_refresh(client: ClientDep) -> dict[str, Any]:
    """Ask the robot for its active fault set; firmware that does not answer falls back to the event-built set."""
    active = lambda: [asdict(a) for a in client.telemetry.hms_alerts]  # noqa: E731
    try:
        snap = await client.system.query_active_faults()
    except NebiRpcTimeoutError:
        return {"ok": True, "source": "events", "active": active(),
                "note": "The robot did not answer the active-fault query (this firmware does not forward it); "
                        "the list is built from fault events received since connecting."}
    return {"ok": True, "source": "snapshot", **snap, "active": active()}


@router.post("/system/hms/clear")
async def api_hms_clear(body: HmsCodeBody, client: ClientDep) -> dict[str, Any]:
    return await client.system.clear_hms_fault(body.code, body.instance)


@router.post("/system/hms/inject_sim_fault")
async def api_hms_inject(body: HmsCodeBody, sim: SimulatorDep) -> dict[str, Any]:
    sim.inject_hms_fault(body.code, body.instance)
    return {"ok": True, "injected": body.code}


# --- Robot Wi-Fi & OTA ---


@router.get("/system/wifi/scan")
async def api_wifi_scan(client: ClientDep) -> dict[str, Any]:
    return {"networks": await client.system.scan_wifi()}


@router.post("/system/wifi/connect")
async def api_wifi_connect(body: WifiConnectBody, client: ClientDep) -> dict[str, Any]:
    return await client.system.connect_wifi(body.ssid, body.password, body.security)


@router.post("/system/wifi/provision")
async def api_wifi_provision(body: WifiProvisionBody, manager: ManagerDep) -> dict[str, Any]:
    # link-mutating (hands the client off to the LAN IP): goes through the single-flight guard
    return await manager.provision_wifi_handoff(body)


@router.post("/system/wifi/station")
async def api_wifi_station(client: ClientDep) -> dict[str, Any]:
    return await client.system.switch_wifi_to_station()


@router.post("/system/wifi/switch_ap")
async def api_wifi_switch_ap(client: ClientDep) -> dict[str, Any]:
    return await client.switch_wifi_to_ap()


@router.post("/system/ota/upload")
async def api_ota_upload(file: Annotated[UploadFile, File()], client: ClientDep) -> dict[str, Any]:
    raw = await file.read()
    return await client.system.start_ota_upgrade(file.filename or "firmware.tar", raw)

