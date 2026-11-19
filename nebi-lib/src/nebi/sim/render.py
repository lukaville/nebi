"""Pillow 3D-perspective FPV renderer: floor grid, charging dock, moving person/pet actors, game beacon."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw

if TYPE_CHECKING:
    from nebi.sim.robot import MockNebiRobot


def world_actors(sim_t: float) -> list[dict[str, Any]]:
    """Moving person + pet actors in world coordinates at simulation time `sim_t`."""
    person_x = 3.2 + 0.9 * math.sin(sim_t * 0.45)
    person_y = 1.1 * math.sin(sim_t * 0.9)
    pet_x = 2.4 + 0.7 * math.cos(sim_t * 0.85)
    pet_y = -0.9 + 0.6 * math.sin(sim_t * 1.1)
    return [
        {"id": 101, "kind": "person", "x": person_x, "y": person_y, "h": 1.65, "w": 0.52, "color": (255, 138, 76)},
        {"id": 202, "kind": "pet", "x": pet_x, "y": pet_y, "h": 0.48, "w": 0.55, "color": (126, 231, 135)},
    ]


def render_fpv_frame(robot: MockNebiRobot, sim_t: float) -> tuple[Image.Image, list[dict[str, Any]]]:
    """Render the FPV camera image and the normalized detection boxes of visible actors."""
    w, h = robot.video_width, robot.video_height
    img = Image.new("RGB", (w, h), (16, 22, 34))
    draw = ImageDraw.Draw(img)

    fov_h = math.radians(68.0 / max(1.0, robot.zoom_x))
    fx = (w * 0.5) / math.tan(fov_h * 0.5)
    fy = fx

    cam_z = (robot.body_height_cm / 100.0) + 0.16 + robot.jump_z_m
    eff_pitch_rad = math.radians(robot.pitch_deg + robot.gimbal_pitch_deg)
    eff_roll_rad = 0.0 if robot.horizon_lock else math.radians(robot.roll_deg)

    horizon_y = int(h * 0.5 + math.tan(eff_pitch_rad) * fy * 0.35)
    horizon_y = max(-h, min(2 * h, horizon_y))

    # Sky & Arena Floor
    ev_boost = int(robot.ev_comp * 18.0) if robot.pro_mode else 0
    wb_warm = int((robot.wb_kelvin - 5500) / 140) if robot.pro_mode else 0
    sky_col = (
        max(8, min(255, 22 + ev_boost + wb_warm)),
        max(10, min(255, 32 + ev_boost)),
        max(18, min(255, 54 + ev_boost - wb_warm)),
    )
    floor_col = (
        max(8, min(255, 27 + ev_boost + wb_warm)),
        max(12, min(255, 35 + ev_boost)),
        max(16, min(255, 46 + ev_boost - wb_warm)),
    )
    roll_dy = int(math.tan(eff_roll_rad) * (w * 0.5))
    draw.polygon([(0, 0), (w, 0), (w, horizon_y + roll_dy), (0, horizon_y - roll_dy)], fill=sky_col)
    draw.polygon([(0, horizon_y - roll_dy), (w, horizon_y + roll_dy), (w, h), (0, h)], fill=floor_col)
    draw.line([(0, horizon_y - roll_dy), (w, horizon_y + roll_dy)], fill=(70, 145, 210), width=2)

    cos_y, sin_y = math.cos(robot.yaw_rad), math.sin(robot.yaw_rad)
    cos_r, sin_r = math.cos(eff_roll_rad), math.sin(eff_roll_rad)
    cos_p, sin_p = math.cos(eff_pitch_rad), math.sin(eff_pitch_rad)

    def project(wx: float, wy: float, wz: float) -> tuple[float, float, float] | None:
        dx = wx - robot.pos_x
        dy = wy - robot.pos_y
        dz = wz - cam_z
        # Body/camera frame: x_fwd, y_left, z_up
        xf = dx * cos_y + dy * sin_y
        yl = -dx * sin_y + dy * cos_y
        zu = dz
        # Pitch
        xf2 = xf * cos_p + zu * sin_p
        zu2 = -xf * sin_p + zu * cos_p
        if xf2 <= 0.22:
            return None
        # Roll
        yl2 = yl * cos_r - zu2 * sin_r
        zu3 = yl * sin_r + zu2 * cos_r
        sx = (w * 0.5) - (yl2 / xf2) * fx
        sy = (h * 0.5) - (zu3 / xf2) * fy
        return sx, sy, xf2

    # 3D perspective floor grid (1m spacing around robot)
    gx0 = math.floor(robot.pos_x) - 7
    gy0 = math.floor(robot.pos_y) - 7
    grid_color = (46, 62, 82)
    for gx in range(gx0, gx0 + 15):
        pts = []
        for gy_step in range(gy0 * 2, (gy0 + 15) * 2):
            p = project(float(gx), gy_step * 0.5, 0.0)
            if p is not None and -w <= p[0] <= 2 * w and -h <= p[1] <= 2 * h:
                pts.append((p[0], p[1]))
        if len(pts) >= 2:
            draw.line(pts, fill=grid_color, width=1)

    for gy in range(gy0, gy0 + 15):
        pts = []
        for gx_step in range(gx0 * 2, (gx0 + 15) * 2):
            p = project(gx_step * 0.5, float(gy), 0.0)
            if p is not None and -w <= p[0] <= 2 * w and -h <= p[1] <= 2 * h:
                pts.append((p[0], p[1]))
        if len(pts) >= 2:
            draw.line(pts, fill=grid_color, width=1)

    # Render Charging Dock + ArUco beacon
    dock_p = project(robot.dock_x, robot.dock_y, 0.22)
    if dock_p is not None:
        dsx, dsy, ddepth = dock_p
        dw = max(14, int((0.55 / ddepth) * fx))
        dh = max(14, int((0.44 / ddepth) * fy))
        draw.rectangle([dsx - dw // 2, dsy - dh // 2, dsx + dw // 2, dsy + dh // 2], fill=(32, 42, 54), outline=(56, 239, 125), width=2)
        # ArUco pattern square
        aw = max(6, dw // 3)
        draw.rectangle([dsx - aw // 2, dsy - aw // 2, dsx + aw // 2, dsy + aw // 2], fill=(240, 245, 250))
        draw.rectangle([dsx - aw // 4, dsy - aw // 4, dsx + aw // 4, dsy + aw // 4], fill=(20, 24, 28))

    # Render Person & Pet 3D actors + compute normalized AI bounding boxes
    boxes: list[dict[str, Any]] = []
    actors = sorted(world_actors(sim_t), key=lambda a: math.hypot(a["x"] - robot.pos_x, a["y"] - robot.pos_y), reverse=True)
    for act in actors:
        if robot.target_type_filter == 1 and act["kind"] != "person":
            continue
        if robot.target_type_filter == 2 and act["kind"] != "pet":
            continue
        foot = project(act["x"], act["y"], 0.0)
        top = project(act["x"], act["y"], act["h"])
        if foot is None or top is None:
            continue
        cx_px = (foot[0] + top[0]) * 0.5
        bh_px = abs(foot[1] - top[1])
        bw_px = (act["w"] / foot[2]) * fx
        x1, y1 = cx_px - bw_px * 0.5, min(top[1], foot[1])
        x2, y2 = cx_px + bw_px * 0.5, max(top[1], foot[1])

        col = act["color"]
        if act["kind"] == "person":
            head_r = max(4, int(bw_px * 0.24))
            draw.ellipse([cx_px - head_r, y1, cx_px + head_r, y1 + head_r * 2], fill=col)
            draw.rounded_rectangle(
                [cx_px - bw_px * 0.32, y1 + head_r * 2.1, cx_px + bw_px * 0.32, y1 + bh_px * 0.68],
                radius=5,
                fill=(68, 138, 255),
            )
            leg_swing = math.sin(sim_t * 5.0) * bw_px * 0.14
            draw.line([(cx_px - bw_px * 0.14, y1 + bh_px * 0.68), (cx_px - bw_px * 0.16 + leg_swing, y2)], fill=(210, 220, 235), width=max(2, int(bw_px * 0.12)))
            draw.line([(cx_px + bw_px * 0.14, y1 + bh_px * 0.68), (cx_px + bw_px * 0.16 - leg_swing, y2)], fill=(210, 220, 235), width=max(2, int(bw_px * 0.12)))
        else:
            # Cute quadruped pet silhouette
            body_y1 = y1 + bh_px * 0.32
            body_y2 = y1 + bh_px * 0.72
            draw.rounded_rectangle([x1 + bw_px * 0.1, body_y1, x2 - bw_px * 0.15, body_y2], radius=6, fill=col)
            hr = max(4, int(bh_px * 0.24))
            draw.ellipse([x2 - bw_px * 0.35, y1 + bh_px * 0.08, x2 - bw_px * 0.35 + hr * 2, y1 + bh_px * 0.08 + hr * 2], fill=col)
            draw.line([(x1 + bw_px * 0.2, body_y2), (x1 + bw_px * 0.2, y2)], fill=col, width=max(2, int(bw_px * 0.09)))
            draw.line([(x2 - bw_px * 0.25, body_y2), (x2 - bw_px * 0.25, y2)], fill=col, width=max(2, int(bw_px * 0.09)))

        # Keep visible boxes within [0..1]
        nx = cx_px / w
        ny = (y1 + y2) * 0.5 / h
        nw = bw_px / w
        nh = bh_px / h
        if 0.02 <= nx <= 0.98 and 0.02 <= ny <= 0.98:
            boxes.append(
                {
                    "id": act["id"],
                    "kind": act["kind"],
                    "cx": max(0.0, min(1.0, nx)),
                    "cy": max(0.0, min(1.0, ny)),
                    "w": max(0.03, min(0.9, nw)),
                    "h": max(0.04, min(0.9, nh)),
                }
            )

    # Render interactive Game beacon if a game is active
    if robot.game_state == 1:
        orb_x = 2.6 * math.cos(sim_t * 0.7)
        orb_y = 2.6 * math.sin(sim_t * 0.7)
        op = project(orb_x, orb_y, 0.55 + 0.15 * math.sin(sim_t * 4.0))
        if op is not None:
            orad = max(8, int((0.28 / op[2]) * fx))
            draw.ellipse([op[0] - orad, op[1] - orad, op[0] + orad, op[1] + orad], fill=(255, 206, 58), outline=(255, 255, 255), width=2)

    return img, boxes
