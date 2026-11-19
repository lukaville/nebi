"""25 Hz kinematic model of the simulated robot: manual drive, autonomous docking/following, jumps, flips."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from nebi.sim.render import world_actors
from nebi.types import CameraSubMode, MissionId, PetSubMode, RobotPostureState

if TYPE_CHECKING:
    from nebi.sim.robot import MockNebiRobot


def step_physics(robot: MockNebiRobot, sim_t: float, dt: float) -> None:
    """Advance pose, posture animations, autonomy (dock / follow), battery and OTA progress by `dt` seconds."""
    max_v = float(robot.config_store.get("md_function.max_speed", 2.0))
    gear_scale = {1: 0.55, 2: 1.0, 3: 1.45}.get(robot.speed_gear, 1.0)
    joy_fresh = (time.monotonic() - robot.last_joystick_ts) < 0.6

    actors = world_actors(sim_t)

    # Autonomous docking
    if robot.mission_id == int(MissionId.PET) and robot.sub_mode == int(PetSubMode.DOCK) and not robot.docked:
        dx = robot.dock_x - robot.pos_x
        dy = robot.dock_y - robot.pos_y
        dist = math.hypot(dx, dy)
        target_yaw = math.atan2(dy, dx)
        yaw_err = (target_yaw - robot.yaw_rad + math.pi) % (2 * math.pi) - math.pi
        robot.yaw_rate_rps = max(-1.4, min(1.4, yaw_err * 2.2))
        robot.vx_mps = min(0.85, dist * 0.7) if abs(yaw_err) < 0.5 else 0.0
        if dist < 0.25:
            robot.docked = True
            robot.charging = True
            robot.vx_mps = 0.0
            robot.yaw_rate_rps = 0.0
            robot.posture_state = RobotPostureState.CHARGING

    # Autonomous target follow / orbit / spotlight
    elif (
        robot.tracking_enabled
        and robot.mission_id == int(MissionId.CAMERA)
        and robot.sub_mode
        in (
            int(CameraSubMode.FOLLOW_TRAIL),
            int(CameraSubMode.FOLLOW_PARALLEL),
            int(CameraSubMode.FOLLOW_LEAD),
            int(CameraSubMode.FOLLOW_ORBIT),
            int(CameraSubMode.SPOTLIGHT),
        )
    ):
        tgt = next((a for a in actors if a["id"] == robot.locked_target_id), actors[0])
        dx = tgt["x"] - robot.pos_x
        dy = tgt["y"] - robot.pos_y
        dist = math.hypot(dx, dy)
        bearing = math.atan2(dy, dx)
        yaw_err = (bearing - robot.yaw_rad + math.pi) % (2 * math.pi) - math.pi
        robot.yaw_rate_rps = max(-1.8, min(1.8, yaw_err * 2.5))
        if robot.sub_mode == int(CameraSubMode.SPOTLIGHT):
            robot.vx_mps = 0.0
            robot.vy_mps = 0.0
        elif robot.sub_mode == int(CameraSubMode.FOLLOW_ORBIT):
            robot.vx_mps = max(-0.8, min(0.8, (dist - robot.follow_distance_m) * 0.8))
            robot.vy_mps = robot.orbit_speed
        else:
            robot.vx_mps = max(-1.2, min(1.6, (dist - robot.follow_distance_m) * 1.1))
            robot.vy_mps = 0.0

    # Manual joystick control
    elif robot.posture_state not in (RobotPostureState.FOLDED, RobotPostureState.CHARGING):
        if joy_fresh:
            target_vx = robot.joystick.ly * max_v * gear_scale
            target_vy = (0.0 if robot.joystick.roll_mode else robot.joystick.lx * 0.8 * gear_scale)
            max_yaw_rad = math.radians(float(robot.config_store.get("md_function.max_yaw_rate", 90.0)))
            target_wz = -robot.joystick.rx * max_yaw_rad * gear_scale
            robot.vx_mps += (target_vx - robot.vx_mps) * min(1.0, dt * 8.0)
            robot.vy_mps += (target_vy - robot.vy_mps) * min(1.0, dt * 8.0)
            robot.yaw_rate_rps += (target_wz - robot.yaw_rate_rps) * min(1.0, dt * 10.0)
            robot.gimbal_pitch_deg = max(-35.0, min(35.0, robot.joystick.ry * 30.0))
            robot.body_height_cm = 8.0 + robot.joystick.height * 6.0
            if robot.joystick.roll_mode:
                robot.roll_deg = robot.joystick.lx * 16.0
            elif abs(robot.joystick.roll) > 0.01:
                robot.roll_deg = robot.joystick.roll * 16.0
            else:
                robot.roll_deg = -robot.yaw_rate_rps * 4.5
        else:
            robot.vx_mps *= 0.82
            robot.vy_mps *= 0.82
            robot.yaw_rate_rps *= 0.80
            robot.roll_deg *= 0.85
    else:
        robot.vx_mps = 0.0
        robot.vy_mps = 0.0
        robot.yaw_rate_rps = 0.0
        robot.body_height_cm = 5.0 if robot.posture_state == RobotPostureState.FOLDED else 7.0

    # 180 turn-around override
    if robot.turn_around_rad_rem > 0.0:
        step = min(robot.turn_around_rad_rem, 4.2 * dt)
        robot.yaw_rad += step
        robot.turn_around_rad_rem -= step

    # Integrate planar pose
    cos_y, sin_y = math.cos(robot.yaw_rad), math.sin(robot.yaw_rad)
    dx_w = (robot.vx_mps * cos_y - robot.vy_mps * sin_y) * dt
    dy_w = (robot.vx_mps * sin_y + robot.vy_mps * cos_y) * dt
    robot.pos_x = max(-12.0, min(12.0, robot.pos_x + dx_w))
    robot.pos_y = max(-12.0, min(12.0, robot.pos_y + dy_w))
    robot.yaw_rad = (robot.yaw_rad + robot.yaw_rate_rps * dt + math.pi) % (2 * math.pi) - math.pi
    step_d = math.hypot(dx_w, dy_w)
    robot.trip_odom_m += step_d
    robot.session_odom_m += step_d

    # Jump vertical physics
    if robot.jump_z_m > 0.0 or robot.jump_vz > 0.0:
        robot.jump_z_m += robot.jump_vz * dt
        robot.jump_vz -= 9.81 * dt
        if robot.jump_z_m <= 0.0:
            robot.jump_z_m = 0.0
            robot.jump_vz = 0.0
            if robot.posture_state == RobotPostureState.JUMPING:
                robot.posture_state = RobotPostureState.BALANCING

    # Flip stunt animation
    if robot.stunt_timer > 0.0:
        robot.stunt_timer = max(0.0, robot.stunt_timer - dt)
        phase = (0.75 - robot.stunt_timer) / 0.75
        if robot.stunt_dir in (1, 2):
            robot.pitch_deg = (360.0 * phase) * (-1.0 if robot.stunt_dir == 1 else 1.0)
        else:
            robot.roll_deg = (360.0 * phase) * (-1.0 if robot.stunt_dir == 3 else 1.0)
        if robot.stunt_timer <= 0.0:
            robot.pitch_deg = 0.0
            robot.roll_deg = 0.0
            robot.posture_state = RobotPostureState.BALANCING
    else:
        # Self-balancing pitch lean proportional to forward velocity
        robot.pitch_deg = -robot.vx_mps * 4.5

    # Battery charge/drain
    if robot.charging:
        robot.battery_soc = min(100.0, robot.battery_soc + dt * 0.15)
    else:
        robot.battery_soc = max(12.0, robot.battery_soc - dt * 0.008)

    # Simulated OTA progression
    if robot.ota_stage == 2:
        robot.ota_progress = min(100, robot.ota_progress + 4)
        if robot.ota_progress >= 100:
            robot.ota_stage = 3
