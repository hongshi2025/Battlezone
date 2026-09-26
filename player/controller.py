"""
玩家控制器 —— 把 InputCommand 应用到 PlayerState 上 (纯函数式, 确定性)。

同一份代码运行于:
  * 服务器 (权威模拟)
  * 客户端 (本地预测)
  * 机器人

动作系统 (每个动作都有独立的状态/计时器, 渲染层据此播放专属动画):
  站立 / 行走 / 冲刺 / 滑铲 / 半蹲 / 趴下(含过渡) / 跳跃 / 翻越(mantle) / 侧身 / 开镜
"""
import math
from core import settings as S
from core.commands import Btn, InputCommand
from core.mathutil import (clamp, approach, forward_vec, right_vec, hash_rand, spread_dir,
                           move_towards)
from .player_state import (PlayerState, STAND, CROUCH, PRONE,
                           M_IDLE, M_WALK, M_SPRINT, M_SLIDE, M_AIR, M_MANTLE)

PITCH_LIMIT = math.radians(88)
PRONE_PITCH_LIMIT = (math.radians(-25), math.radians(40))


class ShotRequest:
    """一次射击 (可能含多颗弹丸) — 由 World 做命中判定"""
    __slots__ = ("pid", "weapon", "origin", "dirs", "counter")

    def __init__(self, pid, weapon, origin, dirs, counter):
        self.pid, self.weapon, self.origin, self.dirs, self.counter = pid, weapon, origin, dirs, counter


def eye_position(p: PlayerState, col=None):
    """眼睛/枪口射线起点 (含侧身与趴下偏移, 并防止穿墙)"""
    base = (p.pos[0], p.pos[1] + p.eye_h, p.pos[2])
    off = [0.0, 0.0, 0.0]
    if abs(p.lean) > 0.01:
        r = right_vec(p.yaw)
        off[0] += r[0] * p.lean * S.LEAN_OFFSET
        off[2] += r[2] * p.lean * S.LEAN_OFFSET
        off[1] -= abs(p.lean) * 0.06
    if p.stance == PRONE:
        f = (math.sin(p.yaw), -math.cos(p.yaw))
        off[0] += f[0] * 0.55
        off[2] += f[1] * 0.55
    if col is not None and (off[0] or off[2]):
        off = col.safe_offset((p.pos[0], base[1], p.pos[2]), tuple(off))
    return (base[0] + off[0], base[1] + off[1], base[2] + off[2])


def _set_stance(p, col, new):
    """尝试切换姿态 (检查头顶空间)"""
    if new == p.stance:
        return True
    h = {STAND: S.BODY_STAND, CROUCH: S.BODY_CROUCH, PRONE: S.BODY_PRONE}[new]
    if h > p.body_height() and not col.body_free(p.pos[0], p.pos[1], p.pos[2], S.PLAYER_RADIUS, h):
        if new == STAND and col.body_free(p.pos[0], p.pos[1], p.pos[2], S.PLAYER_RADIUS, S.BODY_CROUCH):
            new = CROUCH
            if new == p.stance:
                return False
        else:
            return False
    if new == PRONE or p.stance == PRONE:
        p.prone_t = S.PRONE_TRANSITION
    p.stance = new
    return True


def _try_mantle(p, col, fwd):
    target = col.ledge_for_mantle(p.pos[0], p.pos[1], p.pos[2], fwd[0], fwd[1], S.PLAYER_RADIUS,
                                  S.MANTLE_MIN, S.MANTLE_MAX, S.BODY_CROUCH)
    if target is None:
        return False
    p.move = M_MANTLE
    p.mantle_t = S.MANTLE_TIME
    p.mantle_from = tuple(p.pos)
    p.mantle_to = target
    p.vel = [0.0, 0.0, 0.0]
    p.stance = STAND
    p.slide_t = 0.0
    p.jump_counter += 1
    return True


def simulate_player(p: PlayerState, cmd: InputCommand, col, want_shots=True):
    """推进一个 tick。返回 ShotRequest 列表。"""
    shots = []
    if not p.alive:
        p.last_buttons = cmd.buttons
        p.last_seq = cmd.seq
        return shots
    dt = clamp(cmd.dt, 0.0, 0.05)
    btn = cmd.buttons
    pressed = btn & ~p.last_buttons
    released = p.last_buttons & ~btn
    p.last_buttons = btn
    p.last_seq = cmd.seq
    w = p.weapon

    # ── 视角 ──
    p.yaw = cmd.yaw
    p.pitch = clamp(cmd.pitch, -PITCH_LIMIT, PITCH_LIMIT)
    if p.stance == PRONE:
        p.pitch = clamp(p.pitch, PRONE_PITCH_LIMIT[0], PRONE_PITCH_LIMIT[1])

    fwd = (math.sin(p.yaw), -math.cos(p.yaw))
    rgt = (math.cos(p.yaw), math.sin(p.yaw))
    mf = clamp(cmd.move_f, -1, 1)
    mr = clamp(cmd.move_r, -1, 1)
    mlen = math.hypot(mf, mr)
    if mlen > 1:
        mf, mr = mf / mlen, mr / mlen
        mlen = 1.0

    p.slide_cd = max(0.0, p.slide_cd - dt)
    p.prone_t = max(0.0, p.prone_t - dt)
    p.spawn_protect = max(0.0, p.spawn_protect - dt)

    # ═════════════ 武器切换 / 模式 / 检视 ═════════════
    if len(p.weapons) > 1:
        target = None
        if pressed & Btn.PRIMARY and p.active != 0:
            target = 0
        elif pressed & Btn.SECONDARY and p.active != 1:
            target = 1
        elif pressed & Btn.SWAP:
            target = 1 - p.active
        if target is not None and p.move != M_MANTLE:
            w.cancel()
            p.active = target
            w = p.weapon
            w.draw()
    if w:
        if pressed & Btn.FIREMODE:
            w.cycle_fire_mode()
        if pressed & Btn.INSPECT:
            w.start_inspect()

    # ═════════════ 翻越 (mantle) 进行中 ═════════════
    if p.move == M_MANTLE:
        p.mantle_t -= dt
        t = 1.0 - max(0.0, p.mantle_t) / S.MANTLE_TIME
        a, b = p.mantle_from, p.mantle_to
        # 先抬升, 后前推
        ty = min(1.0, t / 0.65)
        ty = 1 - (1 - ty) ** 2
        tx = clamp((t - 0.35) / 0.65, 0, 1)
        p.pos = [a[0] + (b[0] - a[0]) * tx, a[1] + (b[1] - a[1]) * ty, a[2] + (b[2] - a[2]) * tx]
        p.eye_h = approach(p.eye_h, S.EYE_CROUCH + 0.2, S.EYE_LERP_SPEED * dt)
        p.ads = max(0.0, p.ads - dt * 6)
        p.sprint_cool = 0.25
        if p.mantle_t <= 0:
            p.pos = list(b)
            p.move = M_IDLE
            p.on_ground = True
            if not col.body_free(p.pos[0], p.pos[1], p.pos[2], S.PLAYER_RADIUS, S.BODY_STAND):
                p.stance = CROUCH
            p.vel = [fwd[0] * 2.0, 0.0, fwd[1] * 2.0]
        if w:
            w.update(dt, False, False, False, False)
        return shots

    # ═════════════ 姿态输入 ═════════════
    sprinting_now = p.move == M_SPRINT
    if pressed & Btn.CROUCH:
        p.crouch_hold = 0.0
        p.crouch_consumed = False
        if sprinting_now and p.on_ground and p.slide_cd <= 0 and p.hspeed() >= S.SLIDE_MIN_SPEED:
            # ── 冲刺中按 C → 滑铲 ──
            sp = max(p.hspeed(), S.SLIDE_BOOST)
            hs = p.hspeed()
            dx, dz = (p.vel[0] / hs, p.vel[2] / hs) if hs > 0.1 else fwd
            p.vel[0], p.vel[2] = dx * sp, dz * sp
            p.move = M_SLIDE
            p.slide_t = S.SLIDE_DURATION
            p.stance = CROUCH
            p.crouch_consumed = True
        elif p.move == M_SLIDE:
            p.slide_t = 0.0      # 滑铲中再按 C → 提前结束
        elif p.stance == PRONE:
            _set_stance(p, col, CROUCH)
            p.crouch_consumed = True
        elif p.stance == CROUCH:
            _set_stance(p, col, STAND)
            p.crouch_consumed = True
        else:
            _set_stance(p, col, CROUCH)
    if btn & Btn.CROUCH:
        p.crouch_hold += dt
        if (p.crouch_hold >= S.PRONE_HOLD_TIME and not p.crouch_consumed
                and p.stance != PRONE and p.move != M_SLIDE and p.on_ground):
            _set_stance(p, col, PRONE)       # 长按 C → 趴下
            p.crouch_consumed = True
    if pressed & Btn.PRONE:
        if p.stance == PRONE:
            _set_stance(p, col, STAND)
        elif p.on_ground:
            p.slide_t = 0.0
            if p.move == M_SLIDE:
                p.move = M_WALK
            _set_stance(p, col, PRONE)

    # ═════════════ 跳跃 / 翻越 ═════════════
    if pressed & Btn.JUMP:
        if p.stance == PRONE:
            _set_stance(p, col, STAND) or _set_stance(p, col, CROUCH)
        elif p.move == M_SLIDE:
            # 滑铲跳: 保留动量
            if col.body_free(p.pos[0], p.pos[1], p.pos[2], S.PLAYER_RADIUS, S.BODY_STAND):
                p.move = M_AIR
                p.stance = STAND
                p.slide_t = 0.0
                p.vel[1] = S.JUMP_VELOCITY * 0.9
                p.on_ground = False
                p.jump_counter += 1
        elif p.on_ground or p.air_time < 0.1:
            if mf > 0.2 and _try_mantle(p, col, fwd):
                return shots
            if p.stance == CROUCH:
                _set_stance(p, col, STAND)
            else:
                p.vel[1] = S.JUMP_VELOCITY
                p.on_ground = False
                p.move = M_AIR
                p.jump_counter += 1
        else:
            if _try_mantle(p, col, fwd):       # 空中贴墙也可翻越
                return shots
    # 空中持续按住空格贴近矮墙 → 自动翻越
    if not p.on_ground and btn & Btn.JUMP and mf > 0.2 and p.vel[1] < 2.0:
        if _try_mantle(p, col, fwd):
            return shots

    # ═════════════ 冲刺判定 ═════════════
    firing = bool(btn & Btn.FIRE) and w is not None and w.ammo > 0
    want_sprint = (btn & Btn.SPRINT and mf > 0.35 and not (btn & Btn.ADS) and not firing
                   and p.stance != PRONE and p.move != M_SLIDE)
    if want_sprint and p.stance == CROUCH and p.on_ground:
        if not _set_stance(p, col, STAND):
            want_sprint = False

    # ═════════════ 滑铲进行中 ═════════════
    if p.move == M_SLIDE:
        p.slide_t -= dt
        hs = p.hspeed()
        if hs > 0.01:
            nhs = max(0.0, hs - S.SLIDE_FRICTION * dt * (1.0 + (1 - p.slide_t / S.SLIDE_DURATION) * 1.5))
            # 轻微转向
            dx, dz = p.vel[0] / hs, p.vel[2] / hs
            steer = mr * 0.9 * dt
            dx, dz = dx + rgt[0] * steer, dz + rgt[1] * steer
            l = math.hypot(dx, dz)
            p.vel[0], p.vel[2] = dx / l * nhs, dz / l * nhs
        if p.slide_t <= 0 or p.hspeed() < S.CROUCH_SPEED or not p.on_ground:
            p.slide_cd = S.SLIDE_COOLDOWN
            p.slide_t = 0.0
            if not p.on_ground:
                p.move = M_AIR
                _set_stance(p, col, STAND)
            elif btn & Btn.SPRINT and mf > 0.35:
                p.move = M_SPRINT if _set_stance(p, col, STAND) else M_WALK
            else:
                p.move = M_WALK
                p.stance = CROUCH
    else:
        # ═════════════ 常规移动 ═════════════
        if p.stance == PRONE:
            speed = S.PRONE_SPEED if p.prone_t <= 0 else 0.0
        elif p.stance == CROUCH:
            speed = S.CROUCH_SPEED
        elif want_sprint:
            speed = S.SPRINT_SPEED
        else:
            speed = S.WALK_SPEED
        if w:
            speed *= w.stats.move_mult
        if p.ads > 0.3 and not want_sprint:
            speed *= 1.0 - (1.0 - S.ADS_MOVE_MULT) * p.ads
        if want_sprint:
            # 冲刺时横移受限
            wx = fwd[0] * mf + rgt[0] * mr * 0.45
            wz = fwd[1] * mf + rgt[1] * mr * 0.45
        else:
            wx = fwd[0] * mf + rgt[0] * mr
            wz = fwd[1] * mf + rgt[1] * mr
        wl = math.hypot(wx, wz)
        if wl > 1e-5:
            wx, wz = wx / wl * min(1.0, mlen), wz / wl * min(1.0, mlen)
        tvx, tvz = wx * speed, wz * speed
        accel = S.GROUND_ACCEL if p.on_ground else S.AIR_ACCEL
        if p.on_ground and mlen < 0.05:
            accel = S.GROUND_FRICTION * max(p.hspeed(), 1.0)
        dvx, dvz = tvx - p.vel[0], tvz - p.vel[2]
        dl = math.hypot(dvx, dvz)
        mx = accel * dt
        if dl > mx:
            dvx, dvz = dvx / dl * mx, dvz / dl * mx
        if p.on_ground or mlen > 0.05:
            p.vel[0] += dvx
            p.vel[2] += dvz
        if p.on_ground:
            if want_sprint and p.hspeed() > S.WALK_SPEED * 0.8:
                p.move = M_SPRINT
            elif p.hspeed() > 0.3:
                p.move = M_WALK
            else:
                p.move = M_IDLE
        else:
            p.move = M_AIR

    # ═════════════ 物理 & 碰撞 ═════════════
    p.vel[1] += S.GRAVITY * dt
    was_ground = p.on_ground
    fall_speed = -p.vel[1]
    pos, vel, og, stepped = col.move(tuple(p.pos), tuple(p.vel), dt, S.PLAYER_RADIUS, p.body_height(), p.on_ground)
    p.pos, p.vel, p.on_ground = list(pos), list(vel), og
    if p.on_ground:
        if not was_ground and p.air_time > 0.12:
            p.land_counter += 1
            p.land_speed = fall_speed
            if p.move == M_AIR:
                p.move = M_IDLE
        p.air_time = 0.0
    else:
        p.air_time += dt
        if p.move not in (M_SLIDE,):
            p.move = M_AIR
    # 掉出地图保护
    if p.pos[1] < -20:
        p.health = 0

    # ═════════════ 眼高 (平滑姿态过渡) ═════════════
    target_eye = p.target_eye()
    p.eye_h = approach(p.eye_h, target_eye, S.EYE_LERP_SPEED * dt)
    if stepped:
        p.eye_h -= stepped * 0.6     # 上台阶时镜头平滑
        p.eye_h = max(p.eye_h, target_eye - 0.3)
    # 步伐相位 (用于脚步声/动画同步)
    if p.on_ground and p.move != M_SLIDE:
        p.step_phase += p.hspeed() * dt

    # ═════════════ 侧身 ═════════════
    lean_t = 0.0
    if p.move not in (M_SPRINT, M_SLIDE) and p.on_ground:
        if btn & Btn.LEAN_L:
            lean_t -= 1.0
        if btn & Btn.LEAN_R:
            lean_t += 1.0
    p.lean = move_towards(p.lean, lean_t, dt * 6.0)

    # ═════════════ 开镜 ═════════════
    reloading_mag = w is not None and w.state in ("reload", "draw", "attach")
    moving_prone = p.stance == PRONE and (p.hspeed() > 0.3 or p.prone_t > 0)
    p.ads_held = bool(btn & Btn.ADS) and p.move != M_SPRINT and not reloading_mag and not moving_prone
    if w:
        rate = dt / max(0.05, w.stats.ads_time)
        p.ads = min(1.0, p.ads + rate) if p.ads_held else max(0.0, p.ads - rate * 1.3)

    # ═════════════ 冲刺→开火 延迟 ═════════════
    if p.move == M_SPRINT:
        p.sprint_cool = w.defn.sprint_to_fire if w else S.SPRINT_TO_FIRE
    else:
        p.sprint_cool = max(0.0, p.sprint_cool - dt)

    # ═════════════ 后坐力恢复 ═════════════
    if w:
        if w.since_shot > 0.09:
            rec = w.defn.recoil_recovery * dt
            p.recoil_p = approach(p.recoil_p, 0.0, rec)
            p.recoil_y = approach(p.recoil_y, 0.0, rec)

    # ═════════════ 武器 ═════════════
    if w:
        can_fire = (p.move != M_SPRINT and p.sprint_cool <= 0 and p.prone_t <= 0 and not moving_prone)
        if p.spawn_protect > 0 and pressed & Btn.FIRE:
            p.spawn_protect = 0.0          # 开火取消出生保护
        n = w.update(dt, bool(btn & Btn.FIRE), bool(pressed & Btn.FIRE), bool(pressed & Btn.RELOAD), can_fire)
        for _ in range(n):
            shots.append(_make_shot(p, w, col))
    return shots


def current_spread(p: PlayerState, w):
    """当前散布角 (度) — 客户端准星也用它"""
    st = w.stats
    base = st.spread_hip + (st.spread_ads - st.spread_hip) * p.ads
    moving = min(1.5, p.hspeed() / S.WALK_SPEED)
    base += moving * w.defn.spread_move * (1.0 - 0.5 * p.ads)
    if not p.on_ground:
        base += 2.5
    if p.move == M_SLIDE:
        base += 1.0
    mult = {STAND: 1.0, CROUCH: 0.8, PRONE: 0.6}[p.stance]
    return base * mult + w.bloom * (1.0 - 0.5 * p.ads)


def _make_shot(p: PlayerState, w, col):
    d = w.defn
    st = w.stats
    # 子弹沿 "射击前" 的准星方向发出, 后坐力影响下一发
    origin = eye_position(p, col)
    aim = forward_vec(p.yaw + p.recoil_y, p.pitch + p.recoil_p)
    spread = current_spread(p, w)
    dirs = []
    for i in range(st.pellets):
        a = hash_rand(p.pid, w.shot_counter, i, 21)
        b = hash_rand(p.pid, w.shot_counter, i, 22)
        if st.pellets == 1:
            sp = spread
        else:
            sp = max(spread, st.spread_ads) * (0.35 + 0.65 * hash_rand(p.pid, w.shot_counter, i, 23))
        dirs.append(spread_dir(aim, sp, a, b))
    # ── 后坐力 ──
    mult = 1.0
    if p.stance == CROUCH:
        mult *= 0.85
    elif p.stance == PRONE:
        mult *= 0.7
    if st.bipod and p.stance in (CROUCH, PRONE):
        mult *= 0.45
    if w.shots_in_row <= 1:
        mult *= d.recoil_first
    r1 = hash_rand(p.pid, w.shot_counter, 11)
    r2 = hash_rand(p.pid, w.shot_counter, 12)
    p.recoil_p += math.radians(st.recoil_v * mult * (0.85 + 0.3 * r1))
    p.recoil_y += math.radians(st.recoil_h * mult * ((r2 * 2 - 1) + d.recoil_bias))
    p.recoil_p = min(p.recoil_p, math.radians(22))
    return ShotRequest(p.pid, w.id, origin, dirs, w.shot_counter)
