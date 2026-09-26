"""
第一人称武器 & 手臂动画系统 (Viewmodel)。

采用 "分层叠加" 的程序化动画:
    基础层   腰射姿态 ←→ 开镜姿态 (瞄具中心严格对准屏幕中心)
    姿态层   半蹲 / 冲刺 / 滑铲 / 趴下 / 匍匐 / 翻越 / 空中
    动作层   换弹(战术/空仓) / 逐发装填 / 拉栓 / 泵动 / 拔枪 / 改装配件 / 检视
    程序层   步伐摆动 / 呼吸 / 鼠标惯性 / 后坐弹簧 / 落地冲击 / 跳跃
每层独立计算后相加, 部件(弹匣/枪栓/套筒/泵动护木)与左右手位置也由动作层驱动,
手臂使用双骨骼 IK 从肩膀连接到手部。

动画只"读取"同步来的玩家/武器状态 → 远程玩家、预测、回放都能得到一致表现。
"""
import math
import random
import numpy as np
from OpenGL.GL import *
from core import settings as S
from core.mathutil import smoothstep, clamp, lerp
from player.player_state import STAND, CROUCH, PRONE, M_SPRINT, M_SLIDE, M_MANTLE, M_AIR
from .gl_util import draw_box, draw_limb, perspective
from .gun_models import draw_shell

SLEEVE = (0.30, 0.32, 0.25)
SLEEVE_DARK = (0.22, 0.24, 0.19)
GLOVE = (0.11, 0.11, 0.10)

HIP = {
    "rifle": (0.150, -0.160, -0.47),
    "bullpup": (0.150, -0.150, -0.42),
    "sniper": (0.150, -0.165, -0.48),
    "shotgun": (0.150, -0.160, -0.45),
    "pistol": (0.120, -0.140, -0.44),
}


# ── 4x4 矩阵 (列向量约定) ──
def m_trans(x, y, z):
    m = np.identity(4)
    m[:3, 3] = (x, y, z)
    return m


def m_rot(deg, axis):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    m = np.identity(4)
    if axis == 0:
        m[1, 1], m[1, 2], m[2, 1], m[2, 2] = c, -s, s, c
    elif axis == 1:
        m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    else:
        m[0, 0], m[0, 1], m[1, 0], m[1, 1] = c, -s, s, c
    return m


def xform(m, p):
    v = m @ np.array([p[0], p[1], p[2], 1.0])
    return (v[0], v[1], v[2])


def env(t, a, b):
    """在 [a,b] 区间内 0→1 平滑过渡"""
    if b <= a:
        return 1.0 if t >= b else 0.0
    return smoothstep((t - a) / (b - a))


def keyframes(t, keys):
    """keys: [(t, value_tuple)] 升序 — 平滑插值"""
    if t <= keys[0][0]:
        return keys[0][1]
    for i in range(len(keys) - 1):
        t0, v0 = keys[i]
        t1, v1 = keys[i + 1]
        if t <= t1:
            k = smoothstep((t - t0) / max(1e-6, t1 - t0))
            return tuple(a + (b - a) * k for a, b in zip(v0, v1))
    return keys[-1][1]


class Spring:
    def __init__(self, k=180.0, c=18.0, n=1):
        self.k, self.c = k, c
        self.x = [0.0] * n
        self.v = [0.0] * n

    def kick(self, *imp):
        for i, v in enumerate(imp):
            self.v[i] += v

    def step(self, dt):
        for i in range(len(self.x)):
            a = -self.k * self.x[i] - self.c * self.v[i]
            self.v[i] += a * dt
            self.x[i] += self.v[i] * dt


class ViewModel:
    def __init__(self, gun_cache, textures):
        self.cache = gun_cache
        self.tex = textures
        self.t = 0.0
        self.w = dict(ads=0.0, sprint=0.0, slide=0.0, crouch=0.0, prone=0.0, crawl=0.0, air=0.0, mantle=0.0)
        self.bob_phase = 0.0
        self.bob_amp = 0.0
        self.sway = [0.0, 0.0]
        self.kick = Spring(220, 20, 4)     # z后退, pitch, yaw, roll
        self.land = Spring(120, 12, 1)
        self.jump = Spring(90, 11, 1)
        self.last_sc = {}
        self.last_wid = None
        self.last_lc = None
        self.last_jc = None
        self.flash_t = 0.0
        self.flash_rot = 0.0
        self.flash_scale = 1.0
        self.blowback = 0.0
        self.visual_ads = 0.0
        self.model = None
        self.stats = None
        self.team_color = SLEEVE

    # ════════════════ 更新 ════════════════
    def update(self, dt, p, mouse_dx, mouse_dy):
        self.t += dt
        w = p.weapon
        if w is None:
            return
        self.model = self.cache.get(w.id, w.atts)
        self.stats = w.stats
        # ── 姿态权重 ──
        crawl = p.stance == PRONE and (p.hspeed() > 0.2 or p.prone_t > 0)
        targets = dict(
            ads=p.ads,
            sprint=1.0 if p.move == M_SPRINT else 0.0,
            slide=1.0 if p.move == M_SLIDE else 0.0,
            crouch=1.0 if (p.stance == CROUCH and p.move != M_SLIDE) else 0.0,
            prone=1.0 if p.stance == PRONE else 0.0,
            crawl=1.0 if crawl else 0.0,
            air=1.0 if (not p.on_ground and p.move not in (M_SLIDE, M_MANTLE)) else 0.0,
            mantle=1.0 if p.move == M_MANTLE else 0.0,
        )
        rates = dict(ads=30.0, sprint=9.0, slide=12.0, crouch=10.0, prone=6.0, crawl=7.0, air=8.0, mantle=14.0)
        for k, tv in targets.items():
            if k == "ads":
                self.w[k] = tv       # 开镜进度已由模拟层按武器开镜时间计算
            else:
                self.w[k] += (tv - self.w[k]) * min(1.0, rates[k] * dt)
        # ── 步伐摆动 ──
        hs = p.hspeed() if p.on_ground else 0.0
        if p.move == M_SLIDE:
            hs = 0.0
        freq = 1.9 if p.stance != PRONE else 1.2
        self.bob_phase += hs * dt * freq
        amp_t = clamp(hs / S.WALK_SPEED, 0.0, 1.7)
        self.bob_amp += (amp_t - self.bob_amp) * min(1.0, 8 * dt)
        # ── 鼠标惯性 ──
        tx = clamp(-mouse_dx * 0.00018, -0.035, 0.035)
        ty = clamp(mouse_dy * 0.00018, -0.035, 0.035)
        k = min(1.0, 10 * dt)
        self.sway[0] += (tx - self.sway[0]) * k
        self.sway[1] += (ty - self.sway[1]) * k
        # ── 切枪 ──
        if w.id != self.last_wid:
            self.last_wid = w.id
            self.last_sc[w.id] = w.shot_counter
        # ── 开火 ──
        prev = self.last_sc.get(w.id, w.shot_counter)
        if w.shot_counter != prev:
            n = max(1, min(3, w.shot_counter - prev))
            vk = w.defn.visual_kick
            adsk = 1.0 - 0.55 * self.w["ads"]
            for _ in range(n):
                self.kick.kick(1.4 * vk * adsk, 70 * vk * adsk * (0.8 + 0.4 * random.random()),
                               (random.random() - 0.5) * 30 * vk * adsk, (random.random() - 0.5) * 90 * vk * adsk)
            if not w.stats.suppressed:
                self.flash_t = 0.05
                self.flash_rot = random.random() * 360
                self.flash_scale = 0.55 if w.stats.flash_hidden else 1.0
            else:
                self.flash_t = 0.03
                self.flash_scale = 0.18
            self.blowback = 0.07
        self.last_sc[w.id] = w.shot_counter
        # ── 落地 / 跳跃 ──
        if self.last_lc is not None and p.land_counter != self.last_lc:
            self.land.kick(-min(2.2, 0.35 + p.land_speed * 0.16))
        if self.last_jc is not None and p.jump_counter != self.last_jc:
            self.jump.kick(1.2)
        self.last_lc, self.last_jc = p.land_counter, p.jump_counter
        self.kick.step(dt)
        self.land.step(dt)
        self.jump.step(dt)
        self.flash_t = max(0.0, self.flash_t - dt)
        self.blowback = max(0.0, self.blowback - dt)

    # ════════════════ 动作层 ════════════════
    def _action(self, p, w, m):
        """返回 (dpos, drot, parts{}, left_hand, right_hand, extras{})"""
        pos = [0.0, 0.0, 0.0]
        rot = [0.0, 0.0, 0.0]          # pitch, yaw, roll (度)
        parts = dict(mag=(0.0, 0.0, 0.0), mag_vis=True, bolt=0.0, pump=0.0, slide=0.0)
        lh = m.grip_l
        rh = m.grip_r
        ex = dict(shell=False, lh_weight=1.0, cyc=0.0)
        st, t = w.state, w.progress
        kind = m.kind
        pistol = kind == "pistol"

        # 手枪空仓挂机 / 射击后坐
        if pistol:
            locked = w.ammo == 0 and st in ("idle", "reload")
            parts["slide"] = 0.032 if locked else 0.032 * (self.blowback / 0.07)
        elif w.defn.fire_modes[0] not in ("bolt", "pump"):
            parts["bolt"] = 0.03 * (self.blowback / 0.07)

        if st == "draw":
            e = 1 - (1 - t) ** 3
            pos[1] -= 0.28 * (1 - e)
            pos[2] += 0.05 * (1 - e)
            rot[0] -= 55 * (1 - e)
            rot[2] += 25 * (1 - e)
        elif st == "reload":
            empty = w.empty_reload
            if pistol and empty and t < 0.78:
                parts["slide"] = 0.032
            tilt = env(t, 0.0, 0.14) * (1 - env(t, 0.86, 1.0))
            rot[0] += 10 * tilt
            rot[1] += -6 * tilt
            rot[2] += (24 if not pistol else 18) * tilt
            pos[0] += -0.03 * tilt
            pos[1] += 0.025 * tilt
            pos[2] += 0.03 * tilt
            # 弹匣: 拔出 → 消失 → 新弹匣插入
            if t < 0.40:
                mo = env(t, 0.14, 0.34)
                parts["mag"] = (0.0, -0.30 * mo * mo, 0.03 * mo)
                parts["mag_vis"] = t < 0.36
            else:
                mi = env(t, 0.44, 0.64)
                parts["mag"] = (-0.01 * (1 - mi), -0.22 * (1 - mi), 0.04 * (1 - mi))
                parts["mag_vis"] = t > 0.42
            # 插入到位时的冲击
            bump = env(t, 0.62, 0.66) * (1 - env(t, 0.66, 0.74))
            pos[1] += 0.012 * bump
            rot[0] -= 4 * bump
            mp = m.mag_pos
            under = (mp[0] - 0.01, mp[1] - 0.02, mp[2] + 0.01)
            keys = [(0.0, m.grip_l), (0.12, m.grip_l), (0.17, under),
                    (0.34, (under[0], under[1] - 0.3, under[2] + 0.03)),
                    (0.40, (under[0] - 0.05, under[1] - 0.35, under[2] + 0.08)),
                    (0.46, (under[0], under[1] - 0.22, under[2] + 0.04)),
                    (0.64, under), (0.70, under)]
            if empty and not pistol:
                bp = m.bolt_pos
                keys += [(0.76, (bp[0] + 0.01, bp[1], bp[2])), (0.80, (bp[0] + 0.01, bp[1], bp[2] + 0.06)),
                         (0.84, (bp[0] + 0.01, bp[1], bp[2])), (0.95, m.grip_l), (1.0, m.grip_l)]
                parts["bolt"] = 0.06 * env(t, 0.76, 0.80) * (1 - env(t, 0.80, 0.83))
                rot[2] -= 14 * env(t, 0.72, 0.76) * (1 - env(t, 0.84, 0.9))
            elif empty and pistol:
                keys += [(0.76, (0.0, 0.03, 0.03)), (0.80, (0.0, 0.035, 0.07)), (0.92, m.grip_l), (1.0, m.grip_l)]
                parts["slide"] = 0.032 * (1 - env(t, 0.76, 0.8)) if t < 0.8 else 0.0
            else:
                keys += [(0.82, m.grip_l), (1.0, m.grip_l)]
            lh = keyframes(t, keys)
            # 左手拿着弹匣移动 → 弹匣跟随手 (拔出阶段)
        elif st == "shell":
            ph = w.phase
            if ph == "start":
                e = env(t, 0.0, 1.0)
            elif ph == "end":
                e = 1 - env(t, 0.35 if w.empty_reload else 0.0, 1.0)
            else:
                e = 1.0
            rot[2] += -28 * e
            rot[0] += 12 * e
            pos[0] += -0.02 * e
            pos[1] += 0.035 * e
            pos[2] += 0.02 * e
            port = (0.0, -0.03, -0.05)
            below = (0.02, -0.14, 0.05)
            if ph == "loop":
                lh = keyframes(t, [(0.0, below), (0.45, port), (0.6, (port[0], port[1] + 0.01, port[2] - 0.015)),
                                   (1.0, below)])
                ex["shell"] = t < 0.55
            elif ph == "start":
                lh = keyframes(t, [(0.0, m.grip_l), (1.0, below)])
                ex["shell"] = t > 0.6
            else:
                if w.empty_reload:
                    pz = 0.09 * env(t, 0.1, 0.35) * (1 - env(t, 0.4, 0.62))
                    parts["pump"] = pz
                    lh = keyframes(t, [(0.0, below), (0.12, m.pump_pos)])
                    if t > 0.12:
                        lh = (m.pump_pos[0], m.pump_pos[1], m.pump_pos[2] + pz)
                else:
                    lh = keyframes(t, [(0.0, below), (1.0, m.grip_l)])
        elif st == "cycle":
            mode = w.defn.fire_modes[w.mode_idx % len(w.defn.fire_modes)]
            cyc = env(t, 0.0, 0.15) * (1 - env(t, 0.82, 1.0))
            ex["cyc"] = cyc
            if mode == "bolt":
                bp = m.bolt_pos
                rh = keyframes(t, [(0.0, m.grip_r), (0.2, bp), (0.3, (bp[0], bp[1] + 0.02, bp[2])),
                                   (0.5, (bp[0], bp[1] + 0.02, bp[2] + 0.08)), (0.68, (bp[0], bp[1] + 0.02, bp[2])),
                                   (0.78, bp), (1.0, m.grip_r)])
                parts["bolt"] = 0.08 * env(t, 0.3, 0.5) * (1 - env(t, 0.5, 0.68))
                parts["bolt_up"] = 0.02 * env(t, 0.2, 0.3) * (1 - env(t, 0.68, 0.78))
                rot[2] += 12 * cyc
                rot[0] += 5 * cyc
                pos[1] += 0.01 * cyc
            else:   # pump
                pz = 0.1 * env(t, 0.05, 0.4) * (1 - env(t, 0.45, 0.8))
                parts["pump"] = pz
                lh = (m.pump_pos[0], m.pump_pos[1], m.pump_pos[2] + pz)
                rot[2] += 6 * cyc
                rot[0] += 3 * env(t, 0.05, 0.3) * (1 - env(t, 0.4, 0.8))
                pos[2] += 0.015 * cyc
        elif st == "attach":
            e = env(t, 0.0, 0.3) * (1 - env(t, 0.7, 1.0))
            rot[1] += -30 * e
            rot[2] += 28 * e
            rot[0] += 8 * e
            pos[0] += -0.07 * e
            pos[1] += 0.035 * e
            pos[2] += 0.03 * e
            lh = keyframes(t, [(0.0, m.grip_l), (0.3, (0.0, m.rail_y + 0.02, m.rail_z - 0.05)),
                               (0.55, (0.02, m.rail_y + 0.025, m.rail_z + 0.02)), (0.8, m.grip_l), (1.0, m.grip_l)])
        elif st == "inspect":
            e1 = env(t, 0.04, 0.28) * (1 - env(t, 0.42, 0.56))
            e2 = env(t, 0.46, 0.6) * (1 - env(t, 0.84, 1.0))
            rot[1] += 48 * e1 - 25 * e2
            rot[2] += -42 * e1 + 50 * e2
            rot[0] += 10 * e1 + 15 * e2
            pos[0] += -0.09 * (e1 + e2)
            pos[1] += 0.045 * (e1 + e2)
            pos[2] += 0.04 * (e1 + e2)
        if kind == "shotgun" and st not in ("shell", "cycle"):
            lh = (m.pump_pos[0], m.pump_pos[1] - 0.005, m.pump_pos[2])
        return pos, rot, parts, lh, rh, ex

    # ════════════════ 最终姿态 ════════════════
    def compute(self, p):
        w = p.weapon
        m = self.model
        W = self.w
        kind = m.kind
        pistol = kind == "pistol"
        hip = HIP.get(kind, HIP["rifle"])
        ads_pos = (0.0, -m.sight_y, -(m.relief + m.eye_ref_z))
        a_pos, a_rot, parts, lh, rh, ex = self._action(p, w, m)
        # 拉栓时临时降低开镜权重 (镜头短暂离开瞄具)
        ads = W["ads"] * (1 - ex["cyc"] * 0.85)
        e = smoothstep(ads)
        self.visual_ads = ads
        na = 1 - e
        pos = [lerp(hip[i], ads_pos[i], e) for i in range(3)]
        rot = [0.0, 0.0, 0.0]
        # ── 半蹲: 枪身轻微倾斜靠近 ──
        c = W["crouch"] * na
        pos[0] -= 0.012 * c
        pos[1] += 0.008 * c
        rot[2] += 5 * c
        # ── 冲刺: 枪口斜向下压在胸前 (战术冲刺姿态) ──
        s = W["sprint"] * na
        ph = self.bob_phase * math.pi
        if pistol:
            pos[0] += -0.04 * s
            pos[1] += -0.05 * s
            pos[2] += 0.06 * s
            rot[0] += -32 * s
            rot[2] += -12 * s
        else:
            pos[0] += -0.08 * s
            pos[1] += -0.05 * s
            pos[2] += 0.06 * s
            rot[0] += -12 * s
            rot[1] += 40 * s
            rot[2] += -22 * s
        pos[0] += math.sin(ph) * 0.028 * s
        pos[1] += -abs(math.cos(ph)) * 0.024 * s
        rot[2] += math.sin(ph) * 6 * s
        rot[1] += math.cos(ph) * 3 * s
        # ── 滑铲: 枪身侧倾, 手臂稳住 ──
        sl = W["slide"] * (1 - e * 0.6)
        pos[0] += -0.035 * sl
        pos[1] += 0.03 * sl
        pos[2] += 0.02 * sl
        rot[2] += 20 * sl
        rot[0] += 4 * sl
        # ── 趴下 / 匍匐 ──
        pr = W["prone"] * na
        pos[1] += -0.006 * pr
        cr = W["crawl"]
        cph = self.bob_phase * math.pi * 0.8
        pos[0] += 0.02 * cr + math.sin(cph) * 0.035 * cr
        pos[1] += -0.08 * cr + abs(math.sin(cph)) * 0.02 * cr
        pos[2] += 0.05 * cr
        rot[0] += -28 * cr
        rot[1] += 22 * cr + math.sin(cph) * 8 * cr
        rot[2] += -12 * cr
        # ── 翻越 ──
        mt = W["mantle"]
        pos[0] += 0.03 * mt
        pos[1] += -0.13 * mt
        pos[2] += 0.06 * mt
        rot[0] += -38 * mt
        rot[1] += 12 * mt
        rot[2] += 32 * mt
        # ── 空中: 枪身随竖直速度滞后 ──
        air = W["air"]
        vy = clamp(p.vel[1], -8, 8)
        pos[1] += -vy * 0.004 * air * (1 - 0.6 * e)
        rot[0] += vy * 0.7 * air * (1 - 0.6 * e)
        # ── 行走摆动 (8 字形) ──
        bob = self.bob_amp * (1 - s) * (1 - 0.88 * e) * (1 - cr)
        pos[0] += math.sin(ph) * 0.011 * bob
        pos[1] += -abs(math.cos(ph)) * 0.01 * bob
        rot[2] += math.sin(ph) * 1.2 * bob
        # ── 呼吸 ──
        br = 1 - 0.75 * e
        pos[1] += math.sin(self.t * 1.7) * 0.0016 * br
        pos[0] += math.sin(self.t * 0.9) * 0.0008 * br
        rot[2] += math.sin(self.t * 1.1) * 0.4 * br
        # ── 鼠标惯性 ──
        sw = 1 - 0.65 * e
        pos[0] += self.sway[0] * sw
        pos[1] += self.sway[1] * sw
        rot[1] += self.sway[0] * 250 * sw
        rot[0] += self.sway[1] * 250 * sw
        rot[2] += self.sway[0] * 300 * sw
        # ── 落地 / 跳跃 ──
        pos[1] += self.land.x[0] * 0.035 + self.jump.x[0] * 0.02
        rot[0] += self.land.x[0] * 5 - self.jump.x[0] * 4
        # ── 后坐 ──
        kz, kp, ky, kr = self.kick.x
        pos[2] += kz * 0.03
        pos[1] += kz * 0.004
        rot[0] += kp * 0.08
        rot[1] += ky * 0.05
        rot[2] += kr * 0.05
        # ── 动作层 ──
        act_k = 1 - 0.5 * e
        for i in range(3):
            pos[i] += a_pos[i] * act_k
            rot[i] += a_rot[i] * act_k
        return pos, rot, parts, lh, rh, ex

    # ════════════════ 绘制 ════════════════
    def draw(self, p, width, height, hide_for_scope=False):
        w = p.weapon
        if w is None or self.model is None or not p.alive:
            return
        pos, rot, parts, lh, rh, ex = self.compute(p)
        if hide_for_scope:
            return
        m = self.model
        glClear(GL_DEPTH_BUFFER_BIT)
        glMatrixMode(GL_PROJECTION)
        glPushMatrix()
        glLoadIdentity()
        fov = lerp(S.VIEWMODEL_FOV, S.VIEWMODEL_FOV * 0.82, smoothstep(self.visual_ads))
        perspective(fov, width / float(height), 0.01, 10.0)
        glMatrixMode(GL_MODELVIEW)
        glPushMatrix()
        glLoadIdentity()
        glDisable(GL_FOG)
        glEnable(GL_LIGHTING)
        glEnable(GL_LIGHT0)
        glLightfv(GL_LIGHT0, GL_POSITION, (0.4, 0.8, 0.5, 0.0))
        glLightfv(GL_LIGHT0, GL_DIFFUSE, (0.85, 0.82, 0.76, 1))
        glLightfv(GL_LIGHT0, GL_AMBIENT, (0.42, 0.43, 0.46, 1))
        glLightfv(GL_LIGHT0, GL_SPECULAR, (0.3, 0.3, 0.3, 1))
        glEnable(GL_COLOR_MATERIAL)
        glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
        glMaterialfv(GL_FRONT_AND_BACK, GL_SPECULAR, (0.25, 0.25, 0.25, 1))
        glMaterialf(GL_FRONT_AND_BACK, GL_SHININESS, 24)
        glEnable(GL_NORMALIZE)
        glDisable(GL_TEXTURE_2D)
        glEnable(GL_CULL_FACE)

        # 枪械变换
        M = m_trans(*pos) @ m_rot(rot[1], 1) @ m_rot(rot[0], 0) @ m_rot(rot[2], 2)
        glPushMatrix()
        glMultMatrixf(M.T.flatten().tolist())
        m.draw("body")
        if parts["mag_vis"]:
            glPushMatrix()
            glTranslatef(*parts["mag"])
            m.draw("mag")
            glPopMatrix()
        glPushMatrix()
        glTranslatef(0, parts.get("bolt_up", 0.0), parts["bolt"])
        m.draw("bolt")
        glPopMatrix()
        glPushMatrix()
        glTranslatef(0, 0, parts["slide"])
        m.draw("slide")
        glPopMatrix()
        glPushMatrix()
        glTranslatef(0, 0, parts["pump"])
        m.draw("pump")
        glPopMatrix()
        # 手 (枪械空间)
        self._glove(lh, left=True)
        self._glove(rh, left=False)
        if ex["shell"]:
            draw_shell(lh[0] + 0.005, lh[1] + 0.03, lh[2] - 0.01)
        # 镜片
        if m.lists.get("glass"):
            glDisable(GL_LIGHTING)
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glDepthMask(GL_FALSE)
            glColor4f(0.5, 0.75, 0.8, 0.25)
            glDisable(GL_COLOR_MATERIAL)
            self._glass(m)
            glDepthMask(GL_TRUE)
            glEnable(GL_COLOR_MATERIAL)
            glEnable(GL_LIGHTING)
        # 枪口火焰
        if self.flash_t > 0:
            self._muzzle_flash(m)
        glPopMatrix()

        # 手臂 IK (视图空间)
        lhv = xform(M, lh)
        rhv = xform(M, rh)
        pistol = m.kind == "pistol"
        r_sh = (0.24, -0.36, 0.02)
        l_sh = (-0.22 if not pistol else -0.18, -0.38, -0.02)
        self._arm(r_sh, rhv, pole=(0.8, -1.0, 0.2))
        self._arm(l_sh, lhv, pole=(-0.9, -1.0, 0.0))

        glDisable(GL_LIGHTING)
        glDisable(GL_CULL_FACE)
        glPopMatrix()
        glMatrixMode(GL_PROJECTION)
        glPopMatrix()
        glMatrixMode(GL_MODELVIEW)

    def _glass(self, m):
        for prim in m.parts["glass"]:
            _, c, s, col, rx = prim
            glColor4f(col[0], col[1], col[2], 0.28)
            draw_box(c[0], c[1], c[2], s[0], s[1], s[2])

    def _glove(self, h, left):
        glColor3f(*GLOVE)
        x = h[0] + (-0.012 if left else 0.004)
        draw_box(x, h[1] - 0.004, h[2] + 0.004, 0.048, 0.058, 0.07, GLOVE)
        # 手指包住握把
        draw_box(x + (0.018 if left else -0.018), h[1] + 0.012, h[2] - 0.012, 0.016, 0.03, 0.05, GLOVE)

    def _arm(self, shoulder, hand, pole):
        L1, L2 = 0.40, 0.40
        sx, sy, sz = shoulder
        hx, hy, hz = hand[0], hand[1], hand[2] + 0.03
        dx, dy, dz = hx - sx, hy - sy, hz - sz
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        d = max(1e-4, d)
        dd = min(d, L1 + L2 - 1e-3)
        a = (L1 * L1 - L2 * L2 + dd * dd) / (2 * dd)
        h = math.sqrt(max(0.0, L1 * L1 - a * a))
        ux, uy, uz = dx / d, dy / d, dz / d
        # 极向量正交化
        pd = pole[0] * ux + pole[1] * uy + pole[2] * uz
        px, py, pz = pole[0] - ux * pd, pole[1] - uy * pd, pole[2] - uz * pd
        pl = math.sqrt(px * px + py * py + pz * pz) or 1.0
        px, py, pz = px / pl, py / pl, pz / pl
        elbow = (sx + ux * a + px * h, sy + uy * a + py * h, sz + uz * a + pz * h)
        hand_pt = (sx + ux * min(d, L1 + L2), sy + uy * min(d, L1 + L2), sz + uz * min(d, L1 + L2))
        draw_limb(shoulder, elbow, 0.095, 0.095, self.team_color)
        draw_limb(elbow, hand_pt, 0.078, 0.078, SLEEVE_DARK)
        # 袖口
        cuff = (hand_pt[0] + (elbow[0] - hand_pt[0]) * 0.18, hand_pt[1] + (elbow[1] - hand_pt[1]) * 0.18,
                hand_pt[2] + (elbow[2] - hand_pt[2]) * 0.18)
        draw_limb(hand_pt, cuff, 0.07, 0.07, GLOVE)

    def _muzzle_flash(self, m):
        mz = m.muzzle
        glDisable(GL_LIGHTING)
        glDisable(GL_CULL_FACE)
        glEnable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, self.tex["flash"])
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        glDepthMask(GL_FALSE)
        k = self.flash_t / 0.05
        s = (0.07 + 0.05 * random.random()) * self.flash_scale * (0.6 + 0.4 * k)
        glColor4f(1, 0.9, 0.7, 0.95)
        glPushMatrix()
        glTranslatef(mz[0], mz[1], mz[2] - s * 0.3)
        glRotatef(self.flash_rot, 0, 0, 1)
        glBegin(GL_QUADS)
        glTexCoord2f(0, 0); glVertex3f(-s, -s, 0)
        glTexCoord2f(1, 0); glVertex3f(s, -s, 0)
        glTexCoord2f(1, 1); glVertex3f(s, s, 0)
        glTexCoord2f(0, 1); glVertex3f(-s, s, 0)
        L = s * 2.6
        glTexCoord2f(0, 0); glVertex3f(0, -s * 0.6, 0.02)
        glTexCoord2f(1, 0); glVertex3f(0, -s * 0.6, -L)
        glTexCoord2f(1, 1); glVertex3f(0, s * 0.6, -L)
        glTexCoord2f(0, 1); glVertex3f(0, s * 0.6, 0.02)
        glTexCoord2f(0, 0); glVertex3f(-s * 0.6, 0, 0.02)
        glTexCoord2f(1, 0); glVertex3f(-s * 0.6, 0, -L)
        glTexCoord2f(1, 1); glVertex3f(s * 0.6, 0, -L)
        glTexCoord2f(0, 1); glVertex3f(s * 0.6, 0, 0.02)
        glEnd()
        # 光晕
        glBindTexture(GL_TEXTURE_2D, self.tex["glow"])
        g = s * 3.0
        glColor4f(1.0, 0.7, 0.35, 0.5 * k)
        glBegin(GL_QUADS)
        glTexCoord2f(0, 0); glVertex3f(-g, -g, 0)
        glTexCoord2f(1, 0); glVertex3f(g, -g, 0)
        glTexCoord2f(1, 1); glVertex3f(g, g, 0)
        glTexCoord2f(0, 1); glVertex3f(-g, g, 0)
        glEnd()
        glPopMatrix()
        glDepthMask(GL_TRUE)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDisable(GL_TEXTURE_2D)
        glEnable(GL_LIGHTING)
        glEnable(GL_CULL_FACE)
