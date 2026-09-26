"""
第一人称摄像机效果: 头部晃动 / 侧倾 / FOV 动态 / 落地冲击 / 受击抖动
每种动作都有专属的镜头语言:
    冲刺  → FOV 增大, 更大幅度的上下/左右晃动
    滑铲  → 镜头压低 + 侧倾 + FOV 冲击
    趴下  → 镜头贴地, 匍匐时左右摇摆
    侧身  → 横移 + 翻滚角
    落地  → 下沉弹簧
    开镜  → FOV 按瞄具倍率缩放, 晃动抑制
"""
import math
import random
from core import settings as S
from core.mathutil import clamp, smoothstep, lerp
from player.player_state import STAND, CROUCH, PRONE, M_SPRINT, M_SLIDE, M_MANTLE, M_AIR
from .viewmodel import Spring


def zoom_fov(base, zoom):
    return math.degrees(2 * math.atan(math.tan(math.radians(base) / 2) / max(1.0, zoom)))


class CameraRig:
    def __init__(self):
        self.fov = S.FOV_BASE
        self.roll = 0.0
        self.bob_phase = 0.0
        self.bob_amp = 0.0
        self.land = Spring(140, 13, 1)
        self.shake = 0.0
        self.shake_t = 0.0
        self.last_lc = None
        self.pitch_off = 0.0
        self.yaw_off = 0.0
        self.eye_smooth = None
        self.base_fov = S.FOV_BASE

    def hurt(self, amount):
        self.shake = min(1.0, self.shake + amount / 40.0)

    def update(self, dt, p, zoom, visual_ads, eye_raw):
        """返回 (eye, yaw_add, pitch_add, roll, fov) — 偏移量叠加到输入视角上"""
        if self.last_lc is not None and p.land_counter != self.last_lc:
            self.land.kick(-min(2.5, 0.3 + p.land_speed * 0.18))
        self.last_lc = p.land_counter
        self.land.step(dt)

        ads = smoothstep(visual_ads)
        # ── 头部晃动 ──
        hs = p.hspeed() if (p.on_ground and p.move != M_SLIDE) else 0.0
        self.bob_phase += hs * dt * (1.9 if p.stance != PRONE else 1.1)
        if p.move == M_SPRINT:
            amp_t, lat_t = 0.045, 0.03
        elif p.stance == PRONE:
            amp_t, lat_t = 0.012, 0.035
        elif p.stance == CROUCH:
            amp_t, lat_t = 0.014, 0.01
        else:
            amp_t, lat_t = 0.022, 0.014
        k = clamp(hs / S.WALK_SPEED, 0, 1.6) * (1 - 0.85 * ads)
        self.bob_amp += (k - self.bob_amp) * min(1.0, 8 * dt)
        ph = self.bob_phase * math.pi
        bob_y = -abs(math.sin(ph)) * amp_t * self.bob_amp + amp_t * 0.5 * self.bob_amp
        bob_x = math.cos(ph) * lat_t * self.bob_amp

        # ── 翻滚角 (侧身 / 滑铲 / 冲刺 / 横移) ──
        roll_t = -p.lean * S.LEAN_ANGLE
        if p.move == M_SLIDE:
            roll_t += 7.0
        if p.move == M_MANTLE:
            roll_t += 5.0
        if p.move == M_SPRINT:
            roll_t += math.cos(ph) * 1.2
        # 横向速度造成的轻微侧倾
        rx, rz = math.cos(p.yaw), math.sin(p.yaw)
        lat_v = p.vel[0] * rx + p.vel[2] * rz
        roll_t += -lat_v * 0.25 * (1 - ads)
        self.roll += (roll_t - self.roll) * min(1.0, 10 * dt)

        # ── FOV ──
        fov_t = self.base_fov
        if p.move == M_SPRINT:
            fov_t += S.FOV_SPRINT_ADD
        elif p.move == M_SLIDE:
            fov_t += S.FOV_SLIDE_ADD
        fov_t = lerp(fov_t, zoom_fov(self.base_fov, zoom), ads)
        rate = 18.0 if zoom > 2.5 else S.FOV_LERP_SPEED
        self.fov += (fov_t - self.fov) * min(1.0, rate * dt)

        # ── 受击抖动 ──
        self.shake = max(0.0, self.shake - dt * 2.5)
        sh = self.shake
        self.yaw_off = (random.random() - 0.5) * 0.02 * sh
        self.pitch_off = (random.random() - 0.5) * 0.02 * sh
        if p.move == M_MANTLE:
            self.pitch_off -= 0.06

        # ── 眼睛位置 ──
        fx, fz = math.sin(p.yaw), -math.cos(p.yaw)
        eye = (eye_raw[0] + rx * bob_x, eye_raw[1] + bob_y + self.land.x[0] * 0.06, eye_raw[2] + rz * bob_x)
        return eye, self.yaw_off, self.pitch_off, self.roll, self.fov
