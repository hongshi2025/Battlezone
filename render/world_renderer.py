"""
世界渲染: 天空 / 地图 (烘焙光照 + 细分以获得正确雾效) / 远景 / 士兵 / 特效
"""
import math
import random
import numpy as np
from OpenGL.GL import *
from core import settings as S
from core.mathutil import v_norm, clamp, smoothstep
from player.player_state import STAND, CROUCH, PRONE, M_SLIDE, M_SPRINT
from .gl_util import draw_box, draw_limb, billboard, perspective

# 材质: 纹理, 颜色, UV 缩放(米/重复), 映射方式
MATERIALS = {
    "ground": ("asphalt", (0.58, 0.57, 0.54), 5.0, "world"),
    "perimeter": ("concrete", (0.62, 0.60, 0.56), 3.0, "world"),
    "building": ("plaster", (0.70, 0.66, 0.58), 3.0, "world"),
    "interior": ("plaster", (0.78, 0.76, 0.72), 3.0, "world"),
    "roof": ("metal", (0.36, 0.37, 0.39), 3.0, "world"),
    "metal_floor": ("metal", (0.42, 0.42, 0.44), 2.0, "world"),
    "railing": ("stripe", (0.85, 0.80, 0.70), 1.5, "world"),
    "crate": ("crate", (0.95, 0.9, 0.85), 1.0, "box"),
    "crate_long": ("crate", (0.8, 0.85, 0.7), 1.0, "box"),
    "shelf": ("metal", (0.28, 0.34, 0.48), 1.5, "world"),
    "container_red": ("container", (0.64, 0.22, 0.17), 2.6, "world"),
    "container_blue": ("container", (0.20, 0.34, 0.56), 2.6, "world"),
    "container_green": ("container", (0.24, 0.44, 0.30), 2.6, "world"),
    "container_orange": ("container", (0.80, 0.47, 0.16), 2.6, "world"),
    "container_gray": ("container", (0.52, 0.54, 0.56), 2.6, "world"),
    "barrier": ("concrete", (0.78, 0.77, 0.73), 1.5, "world"),
    "sandbag": ("sandbag", (0.78, 0.70, 0.54), 1.2, "world"),
    "lowwall": ("concrete", (0.60, 0.59, 0.55), 2.0, "world"),
    "tower": ("concrete", (0.56, 0.56, 0.53), 3.0, "world"),
    "metal": ("metal", (0.35, 0.35, 0.36), 1.5, "world"),
    "crane": ("stripe", (0.90, 0.72, 0.22), 3.0, "world"),
    "desk": ("crate", (0.55, 0.45, 0.35), 1.0, "box"),
    "concrete": ("concrete", (0.6, 0.6, 0.58), 3.0, "world"),
}

SUN = v_norm(S.SUN_DIR)


def _face_light(n, y_lo, y_hi, is_top):
    d = max(0.0, n[0] * SUN[0] + n[1] * SUN[1] + n[2] * SUN[2])
    sky = 0.5 + 0.5 * n[1]
    return 0.42 + 0.40 * d + 0.18 * sky


class WorldRenderer:
    def __init__(self, world, textures):
        self.world = world
        self.tex = textures
        self.map_lists = {}
        self._build_map()
        self._build_skyline()
        self._build_sky()
        self.tracers = []
        self.particles = []
        self.decals = []
        self.flashes = []
        self.corpses = []
        self.ents = {}       # 渲染用的平滑状态
        self.cam_right = (1, 0, 0)
        self.cam_up = (0, 1, 0)
        self.cam_pos = (0, 0, 0)
        self.proj = None

    # ════════════════ 地图 ════════════════
    def _build_map(self):
        by_tex = {}
        for b in self.world.map.boxes:
            mat = MATERIALS.get(b.mat, MATERIALS["concrete"])
            by_tex.setdefault(mat[0], []).append((b, mat))
        for tex_name, items in by_tex.items():
            lid = glGenLists(1)
            glNewList(lid, GL_COMPILE)
            glBegin(GL_QUADS)
            for b, mat in items:
                self._emit_box(b, mat)
            glEnd()
            # 地面标线
            if tex_name == "asphalt":
                pass
            glEndList()
            self.map_lists[tex_name] = lid
        # 地面装饰标线 (无纹理)
        lid = glGenLists(1)
        glNewList(lid, GL_COMPILE)
        glBegin(GL_QUADS)
        glColor3f(0.82, 0.68, 0.22)
        y = 0.012
        for x in (-20.0, 20.0):
            for z0 in range(-50, 50, 6):
                glVertex3f(x - 0.08, y, z0); glVertex3f(x - 0.08, y, z0 + 3.5)
                glVertex3f(x + 0.08, y, z0 + 3.5); glVertex3f(x + 0.08, y, z0)
        glColor3f(0.75, 0.75, 0.72)
        for z in (-28.0, 28.0):
            for x0 in range(-56, 56, 5):
                glVertex3f(x0, y, z - 0.1); glVertex3f(x0, y, z + 0.1)
                glVertex3f(x0 + 3, y, z + 0.1); glVertex3f(x0 + 3, y, z - 0.1)
        # 出生区地面
        for zc, col in ((55.5, (0.25, 0.45, 0.75)), (-55.5, (0.75, 0.3, 0.25))):
            glColor3f(*col)
            glVertex3f(-17, y, zc - 3.5); glVertex3f(-17, y, zc + 3.5)
            glVertex3f(-16.6, y, zc + 3.5); glVertex3f(-16.6, y, zc - 3.5)
            glVertex3f(16.6, y, zc - 3.5); glVertex3f(16.6, y, zc + 3.5)
            glVertex3f(17, y, zc + 3.5); glVertex3f(17, y, zc - 3.5)
        glEnd()
        glEndList()
        self.decor_list = lid

    def _emit_box(self, b, mat):
        _, col, uvs, mapping = mat
        faces = (
            ((1, 0, 0), b.maxx, "x"), ((-1, 0, 0), b.minx, "x"),
            ((0, 1, 0), b.maxy, "y"), ((0, -1, 0), b.miny, "y"),
            ((0, 0, 1), b.maxz, "z"), ((0, 0, -1), b.minz, "z"),
        )
        for n, c, axis in faces:
            if axis == "y" and n[1] < 0 and b.miny <= 0.001:
                continue      # 贴地底面不可见
            if axis == "x":
                u0, u1, v0, v1 = b.minz, b.maxz, b.miny, b.maxy
            elif axis == "y":
                u0, u1, v0, v1 = b.minx, b.maxx, b.minz, b.maxz
            else:
                u0, u1, v0, v1 = b.minx, b.maxx, b.miny, b.maxy
            nu = max(1, int(math.ceil((u1 - u0) / 6.0)))
            nv = max(1, int(math.ceil((v1 - v0) / 6.0)))
            base = _face_light(n, b.miny, b.maxy, axis == "y")
            for i in range(nu):
                ua, ub = u0 + (u1 - u0) * i / nu, u0 + (u1 - u0) * (i + 1) / nu
                for j in range(nv):
                    va, vb = v0 + (v1 - v0) * j / nv, v0 + (v1 - v0) * (j + 1) / nv
                    quad = [(ua, va), (ub, va), (ub, vb), (ua, vb)]
                    # 自动校正绕序: (u,v) 叉积方向与法线一致则逆时针
                    # x面: u=z,v=y → u×v = z×y = -x ; y面: u=x,v=z → x×z = -y ; z面: u=x,v=y → +z
                    sign = {"x": -1, "y": -1, "z": 1}[axis] * (n[0] + n[1] + n[2])
                    if sign < 0:
                        quad = quad[::-1]
                    for (u, v) in quad:
                        if axis == "x":
                            p = (c, v, u)
                        elif axis == "y":
                            p = (u, c, v)
                        else:
                            p = (u, v, c)
                        # 伪 AO: 靠近地面的顶点变暗
                        ao = 1.0
                        if axis != "y":
                            ao = 0.72 + 0.28 * clamp(p[1] / 1.2, 0, 1) if p[1] < 1.2 else 1.0
                        k = base * ao
                        glColor3f(col[0] * k, col[1] * k, col[2] * k)
                        if mapping == "box":
                            tu = (u - u0) / max(1e-6, (u1 - u0))
                            tv = (v - v0) / max(1e-6, (v1 - v0))
                            glTexCoord2f(tu, tv)
                        else:
                            glTexCoord2f(u / uvs, v / uvs)
                        glVertex3f(*p)

    def _build_skyline(self):
        """远景城市剪影 (在围墙外)"""
        rng = random.Random(7)
        self.skyline = glGenLists(1)
        glNewList(self.skyline, GL_COMPILE)
        glBegin(GL_QUADS)
        for i in range(90):
            ang = i / 90 * math.pi * 2 + rng.uniform(-0.02, 0.02)
            r = rng.uniform(95, 170)
            cx, cz = math.cos(ang) * r, math.sin(ang) * r
            w = rng.uniform(8, 20)
            h = rng.uniform(12, 60) if rng.random() < 0.7 else rng.uniform(60, 110)
            g = rng.uniform(0.35, 0.5)
            col = (g, g * 1.0, g * 1.05)
            from world.collision import Box
            b = Box(cx - w / 2, 0, cz - w / 2, cx + w / 2, h, cz + w / 2)
            self._emit_box(b, ("flat", col, 10, "world"))
        glEnd()
        glEndList()

    def _build_sky(self):
        self.sky = glGenLists(1)
        glNewList(self.sky, GL_COMPILE)
        top, hor = S.SKY_TOP, S.SKY_HORIZON
        rings, segs = 10, 32
        for r in range(rings):
            a0 = r / rings * math.pi / 2
            a1 = (r + 1) / rings * math.pi / 2
            glBegin(GL_QUAD_STRIP)
            for s in range(segs + 1):
                th = s / segs * math.pi * 2
                for a in (a0, a1):
                    t = math.sin(a) ** 0.6
                    glColor3f(hor[0] + (top[0] - hor[0]) * t, hor[1] + (top[1] - hor[1]) * t,
                              hor[2] + (top[2] - hor[2]) * t)
                    glVertex3f(math.cos(th) * math.cos(a) * 400, math.sin(a) * 400 - 30,
                               math.sin(th) * math.cos(a) * 400)
            glEnd()
        # 地平线下方
        glBegin(GL_QUAD_STRIP)
        for s in range(segs + 1):
            th = s / segs * math.pi * 2
            glColor3f(*S.FOG_COLOR)
            glVertex3f(math.cos(th) * 400, -30, math.sin(th) * 400)
            glVertex3f(math.cos(th) * 400, -200, math.sin(th) * 400)
        glEnd()
        glEndList()

    # ════════════════ 主绘制 ════════════════
    def setup_camera(self, eye, yaw, pitch, roll, fov, w, h):
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        perspective(fov, w / float(h), 0.05, 500.0)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        glRotatef(-roll, 0, 0, 1)
        glRotatef(-math.degrees(pitch), 1, 0, 0)
        glRotatef(math.degrees(yaw), 0, 1, 0)
        glTranslatef(-eye[0], -eye[1], -eye[2])
        mv = np.array(glGetDoublev(GL_MODELVIEW_MATRIX), dtype=np.float64).reshape(4, 4)
        pr = np.array(glGetDoublev(GL_PROJECTION_MATRIX), dtype=np.float64).reshape(4, 4)
        # 摄像机右/上向量 (用于公告板)
        self.cam_right = (mv[0][0], mv[1][0], mv[2][0])
        self.cam_up = (mv[0][1], mv[1][1], mv[2][1])
        self.cam_pos = eye
        # OpenGL 列主序 → 行向量乘法: clip = v @ mv @ pr
        self.proj = (mv, pr, (w, h))

    def project(self, p):
        """世界坐标 → 屏幕坐标 (左上原点), 在摄像机后方则返回 None"""
        if self.proj is None:
            return None
        mv, pr, (w, h) = self.proj
        v = np.array([p[0], p[1], p[2], 1.0])
        eye = v @ mv
        if eye[2] > -0.1:
            return None
        clip = eye @ pr
        if abs(clip[3]) < 1e-6:
            return None
        nx, ny = clip[0] / clip[3], clip[1] / clip[3]
        return ((nx * 0.5 + 0.5) * w, (1 - (ny * 0.5 + 0.5)) * h, -eye[2])

    def draw_world(self, eye):
        glClearColor(*S.FOG_COLOR, 1)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glDisable(GL_LIGHTING)
        glDisable(GL_TEXTURE_2D)
        # 天空 (跟随摄像机)
        glDepthMask(GL_FALSE)
        glDisable(GL_FOG)
        glPushMatrix()
        glTranslatef(eye[0], eye[1], eye[2])
        glCallList(self.sky)
        # 太阳
        glEnable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, self.tex["glow"])
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        sp = (SUN[0] * 300, SUN[1] * 300, SUN[2] * 300)
        glBegin(GL_QUADS)
        glColor4f(1.0, 0.92, 0.75, 0.9)
        billboard(sp, 60, self.cam_right, self.cam_up)
        glColor4f(1.0, 0.8, 0.6, 0.35)
        billboard(sp, 220, self.cam_right, self.cam_up)
        glEnd()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDisable(GL_TEXTURE_2D)
        glPopMatrix()
        glDepthMask(GL_TRUE)

        # 雾
        glEnable(GL_FOG)
        glFogi(GL_FOG_MODE, GL_LINEAR)
        glFogfv(GL_FOG_COLOR, (*S.FOG_COLOR, 1.0))
        glFogf(GL_FOG_START, S.FOG_START)
        glFogf(GL_FOG_END, S.FOG_END)
        glHint(GL_FOG_HINT, GL_NICEST)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        glCullFace(GL_BACK)
        glFrontFace(GL_CCW)
        # 远景
        glFogf(GL_FOG_START, 20.0)
        glFogf(GL_FOG_END, 260.0)
        glDisable(GL_CULL_FACE)
        glCallList(self.skyline)
        glFogf(GL_FOG_START, S.FOG_START)
        glFogf(GL_FOG_END, S.FOG_END)
        # 地图
        glEnable(GL_TEXTURE_2D)
        glTexEnvi(GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_MODULATE)
        for tex_name, lid in self.map_lists.items():
            glBindTexture(GL_TEXTURE_2D, self.tex[tex_name])
            glCallList(lid)
        glDisable(GL_TEXTURE_2D)
        glEnable(GL_POLYGON_OFFSET_FILL)
        glPolygonOffset(-1, -1)
        glCallList(self.decor_list)
        glDisable(GL_POLYGON_OFFSET_FILL)

    # ════════════════ 士兵 ════════════════
    def update_entities(self, dt, world, local_pid, alpha_positions):
        for pid, p in world.players.items():
            e = self.ents.get(pid)
            tp = alpha_positions.get(pid, p.pos)
            if e is None:
                e = self.ents[pid] = dict(pos=list(tp), yaw=p.yaw, phase=0.0, alive=p.alive, fire_t=0.0,
                                          spotted=0.0, eye=p.eye_h)
            if p.alive and not e["alive"]:
                e["pos"] = list(tp)
            k = min(1.0, dt * 20) if pid != local_pid else 1.0
            for i in range(3):
                e["pos"][i] += (tp[i] - e["pos"][i]) * k
            dy = (p.yaw - e["yaw"] + math.pi) % (2 * math.pi) - math.pi
            e["yaw"] += dy * min(1.0, dt * 15)
            e["eye"] += (p.eye_h - e["eye"]) * min(1.0, dt * 12)
            e["phase"] += p.hspeed() * dt * 1.6
            e["alive"] = p.alive
            e["fire_t"] = max(0.0, e["fire_t"] - dt)
            e["spotted"] = max(0.0, e["spotted"] - dt)
        for pid in list(self.ents):
            if pid not in world.players:
                del self.ents[pid]

    def add_corpse(self, p, pos):
        e = self.ents.get(p.pid)
        self.corpses.append(dict(pos=list(pos), yaw=e["yaw"] if e else p.yaw, t=0.0, team=p.team,
                                 stance=p.stance, wid=p.weapon.id if p.weapon else None,
                                 atts=dict(p.weapon.atts) if p.weapon else None, side=random.choice((-1, 1))))
        if len(self.corpses) > 12:
            self.corpses.pop(0)

    def draw_soldiers(self, world, local_pid, gun_cache, dt, show_local=False):
        glEnable(GL_LIGHTING)
        glEnable(GL_LIGHT0)
        glLightfv(GL_LIGHT0, GL_POSITION, (SUN[0], SUN[1], SUN[2], 0.0))
        glLightfv(GL_LIGHT0, GL_DIFFUSE, (0.75, 0.72, 0.66, 1))
        glLightfv(GL_LIGHT0, GL_AMBIENT, (0.48, 0.48, 0.5, 1))
        glLightfv(GL_LIGHT0, GL_SPECULAR, (0, 0, 0, 1))
        glEnable(GL_COLOR_MATERIAL)
        glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
        glEnable(GL_NORMALIZE)
        glDisable(GL_TEXTURE_2D)
        glDisable(GL_CULL_FACE)
        for pid, p in world.players.items():
            if not p.alive or (pid == local_pid and not show_local):
                continue
            e = self.ents.get(pid)
            if e is None:
                continue
            draw_soldier(p, e, gun_cache)
        for c in self.corpses:
            c["t"] += dt
            draw_corpse(c, gun_cache)
        self.corpses = [c for c in self.corpses if c["t"] < 9.0]
        glDisable(GL_LIGHTING)

    # ════════════════ 特效 ════════════════
    def add_tracer(self, o, end, color, visible_start=True):
        d = math.dist(o, end)
        self.tracers.append(dict(o=o, e=end, d=d, t=0.0, c=color))

    def add_impact(self, pos, normal, kind="wall", count=6):
        n = normal or (0, 1, 0)
        for _ in range(count if kind != "blood" else 8):
            v = [n[0] * random.uniform(1, 4) + random.uniform(-2, 2),
                 n[1] * random.uniform(1, 4) + random.uniform(0, 3),
                 n[2] * random.uniform(1, 4) + random.uniform(-2, 2)]
            if kind == "blood":
                col = (0.55, 0.05, 0.03)
                self.particles.append(dict(p=list(pos), v=v, t=0.0, life=0.45, s=0.08, c=col, tex="smoke", g=-6))
            else:
                self.particles.append(dict(p=list(pos), v=v, t=0.0, life=0.25, s=0.035, c=(1.0, 0.8, 0.4),
                                           tex="glow", g=-12, add=True))
        if kind != "blood":
            self.particles.append(dict(p=list(pos), v=[n[0] * 0.6, 0.4 + n[1] * 0.4, n[2] * 0.6], t=0.0,
                                       life=0.9, s=0.35, c=(0.62, 0.6, 0.55), tex="smoke", g=0.2, grow=1.2))
            if normal:
                self.decals.append((pos, normal))
                if len(self.decals) > 180:
                    self.decals.pop(0)

    def add_world_flash(self, pos, scale=1.0):
        self.flashes.append(dict(p=pos, t=0.0, s=scale))

    def update_effects(self, dt):
        for t in self.tracers:
            t["t"] += dt
        self.tracers = [t for t in self.tracers if t["t"] * 700 < t["d"] + 12]
        for p in self.particles:
            p["t"] += dt
            p["v"][1] += p["g"] * dt
            for i in range(3):
                p["p"][i] += p["v"][i] * dt
            if p.get("grow"):
                p["s"] += p["grow"] * dt
        self.particles = [p for p in self.particles if p["t"] < p["life"]]
        for f in self.flashes:
            f["t"] += dt
        self.flashes = [f for f in self.flashes if f["t"] < 0.06]

    def draw_effects(self):
        glDisable(GL_LIGHTING)
        glDisable(GL_CULL_FACE)
        glEnable(GL_BLEND)
        # 弹孔
        glEnable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, self.tex["hole"])
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(GL_FALSE)
        glEnable(GL_POLYGON_OFFSET_FILL)
        glPolygonOffset(-2, -2)
        glColor4f(1, 1, 1, 0.9)
        glBegin(GL_QUADS)
        for pos, n in self.decals:
            s = 0.07
            if abs(n[1]) > 0.5:
                a, b = (1, 0, 0), (0, 0, 1)
            elif abs(n[0]) > 0.5:
                a, b = (0, 1, 0), (0, 0, 1)
            else:
                a, b = (1, 0, 0), (0, 1, 0)
            c = (pos[0] + n[0] * 0.005, pos[1] + n[1] * 0.005, pos[2] + n[2] * 0.005)
            glTexCoord2f(0, 0); glVertex3f(c[0] - a[0] * s - b[0] * s, c[1] - a[1] * s - b[1] * s, c[2] - a[2] * s - b[2] * s)
            glTexCoord2f(1, 0); glVertex3f(c[0] + a[0] * s - b[0] * s, c[1] + a[1] * s - b[1] * s, c[2] + a[2] * s - b[2] * s)
            glTexCoord2f(1, 1); glVertex3f(c[0] + a[0] * s + b[0] * s, c[1] + a[1] * s + b[1] * s, c[2] + a[2] * s + b[2] * s)
            glTexCoord2f(0, 1); glVertex3f(c[0] - a[0] * s + b[0] * s, c[1] - a[1] * s + b[1] * s, c[2] - a[2] * s + b[2] * s)
        glEnd()
        glDisable(GL_POLYGON_OFFSET_FILL)
        # 粒子
        for tex_name, additive in (("smoke", False), ("glow", True)):
            glBindTexture(GL_TEXTURE_2D, self.tex[tex_name])
            glBlendFunc(GL_SRC_ALPHA, GL_ONE if additive else GL_ONE_MINUS_SRC_ALPHA)
            glBegin(GL_QUADS)
            for p in self.particles:
                if p["tex"] != tex_name:
                    continue
                a = 1 - p["t"] / p["life"]
                glColor4f(p["c"][0], p["c"][1], p["c"][2], a * (0.7 if not additive else 1.0))
                billboard(p["p"], p["s"], self.cam_right, self.cam_up, p["t"] * 2)
            for f in self.flashes if tex_name == "glow" else []:
                glColor4f(1, 0.8, 0.45, 1 - f["t"] / 0.06)
                billboard(f["p"], 0.6 * f["s"], self.cam_right, self.cam_up)
            glEnd()
        glDisable(GL_TEXTURE_2D)
        # 曳光弹
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        glLineWidth(2.0)
        glBegin(GL_LINES)
        for t in self.tracers:
            head = min(t["d"], t["t"] * 700)
            tail = max(0.0, head - 7.0)
            if head <= 0.5:
                continue
            d = t["d"] or 1
            o, e = t["o"], t["e"]
            h = [o[i] + (e[i] - o[i]) * head / d for i in range(3)]
            tl = [o[i] + (e[i] - o[i]) * tail / d for i in range(3)]
            c = t["c"]
            glColor4f(c[0], c[1], c[2], 0.0)
            glVertex3f(*tl)
            glColor4f(c[0], c[1], c[2], 0.9)
            glVertex3f(*h)
        glEnd()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(GL_TRUE)


# ════════════════════════ 士兵模型 ════════════════════════
UNIFORM = {0: (0.36, 0.38, 0.30), 1: (0.40, 0.36, 0.28)}
VEST = {0: (0.22, 0.25, 0.20), 1: (0.26, 0.22, 0.18)}
SKIN = (0.72, 0.56, 0.44)
BOOT = (0.12, 0.11, 0.10)


def _arm_ik(sh, hand, L1=0.3, L2=0.3, pole=(0, -1, 0)):
    dx, dy, dz = hand[0] - sh[0], hand[1] - sh[1], hand[2] - sh[2]
    d = max(1e-4, math.sqrt(dx * dx + dy * dy + dz * dz))
    dd = min(d, L1 + L2 - 1e-3)
    a = (L1 * L1 - L2 * L2 + dd * dd) / (2 * dd)
    h = math.sqrt(max(0.0, L1 * L1 - a * a))
    u = (dx / d, dy / d, dz / d)
    pd = pole[0] * u[0] + pole[1] * u[1] + pole[2] * u[2]
    pv = (pole[0] - u[0] * pd, pole[1] - u[1] * pd, pole[2] - u[2] * pd)
    pl = math.sqrt(sum(c * c for c in pv)) or 1
    pv = tuple(c / pl for c in pv)
    return tuple(sh[i] + u[i] * a + pv[i] * h for i in range(3))


def _leg(hip, foot, knee_fwd, col):
    knee = _arm_ik(hip, foot, 0.46, 0.46, pole=(0, 0.2, -1.0 * knee_fwd))
    draw_limb(hip, knee, 0.16, 0.16, col)
    draw_limb(knee, foot, 0.13, 0.13, col)
    draw_box(foot[0], foot[1] + 0.04, foot[2] - 0.05, 0.12, 0.09, 0.26, BOOT)


def _gun(gun_cache, wid, atts):
    if not wid:
        return
    m = gun_cache.get(wid, atts)
    m.draw("body")
    m.draw("mag")
    m.draw("bolt")
    m.draw("slide")
    m.draw("pump")


def draw_soldier(p, e, gun_cache):
    team = p.team
    U, V = UNIFORM[team], VEST[team]
    tc = S.TEAM_COLORS[team]
    x, y, z = e["pos"]
    glPushMatrix()
    glTranslatef(x, y, z)
    glRotatef(-math.degrees(e["yaw"]), 0, 1, 0)
    ph = e["phase"] * math.pi
    speed = min(1.5, p.hspeed() / S.WALK_SPEED)
    sprint = p.move == M_SPRINT
    w = p.weapon
    pitch = p.pitch + p.recoil_p
    if p.stance == PRONE and p.move != M_SLIDE:
        # ── 趴下 ──
        crawl = math.sin(ph * 0.8) * 0.12 * speed
        draw_box(0, 0.2, 0.0, 0.42, 0.22, 0.62, U)
        draw_box(0, 0.26, -0.05, 0.44, 0.14, 0.5, V)
        draw_box(0, 0.3, -0.48, 0.2, 0.2, 0.22, SKIN)
        draw_box(0, 0.36, -0.5, 0.25, 0.13, 0.27, V)
        for s in (-1, 1):
            draw_limb((s * 0.12, 0.15, 0.3), (s * 0.18, 0.12, 0.75 + crawl * s), 0.15, 0.13, U)
            draw_limb((s * 0.18, 0.12, 0.75 + crawl * s), (s * 0.2, 0.1, 1.15 + crawl * s), 0.12, 0.12, U)
        glPushMatrix()
        glTranslatef(0.06, 0.24, -0.62)
        glRotatef(math.degrees(pitch) * 0.5, 1, 0, 0)
        _gun(gun_cache, w.id if w else None, w.atts if w else None)
        glPopMatrix()
        draw_limb((0.2, 0.25, -0.2), (0.06, 0.2, -0.62), 0.09, 0.09, U)
        draw_limb((-0.2, 0.25, -0.2), (0.0, 0.22, -0.9), 0.09, 0.09, U)
        glPopMatrix()
        return
    # ── 站立 / 半蹲 / 滑铲 ──
    eye = e["eye"]
    crouch = clamp((S.EYE_STAND - eye) / (S.EYE_STAND - S.EYE_CROUCH), 0, 1.3)
    slide = p.move == M_SLIDE
    hip_y = 0.95 - 0.38 * min(1.0, crouch)
    if slide:
        hip_y = 0.35
    lean_fwd = 8 * crouch + (18 if sprint else 0) - (35 if slide else 0)
    swing = (0.55 if sprint else 0.35) * speed if p.on_ground and not slide else 0.0
    if not p.on_ground and not slide:
        swing = 0.0
    # 腿
    for s in (-1, 1):
        hip = (s * 0.11, hip_y, 0.0)
        if slide:
            foot = (s * 0.15, 0.05, -0.75 if s > 0 else -0.45)
            knee_f = 1
        elif crouch > 0.5:
            fz = math.sin(ph + (0 if s > 0 else math.pi)) * swing * 0.4
            foot = (s * 0.16, 0.0, (-0.22 if s > 0 else 0.25) + fz)
            knee_f = 1
        else:
            fz = math.sin(ph + (0 if s > 0 else math.pi)) * swing
            lift = max(0.0, math.cos(ph + (0 if s > 0 else math.pi))) * 0.15 * speed
            foot = (s * 0.12, lift + (0.25 if not p.on_ground else 0.0), fz)
            knee_f = 1
        _leg(hip, foot, knee_f, U)
    # 躯干 (绕髋部前倾)
    glPushMatrix()
    glTranslatef(0, hip_y, 0)
    glRotatef(-lean_fwd, 1, 0, 0)
    glRotatef(math.degrees(-p.lean * 0.3), 0, 0, 1)
    draw_box(0, 0.28, 0, 0.40, 0.56, 0.24, U)
    draw_box(0, 0.32, -0.01, 0.43, 0.4, 0.28, V)
    draw_box(0, 0.36, 0.16, 0.3, 0.3, 0.1, V)                  # 背包
    draw_box(0.215, 0.44, 0, 0.012, 0.08, 0.1, tc)              # 队伍臂章
    draw_box(-0.215, 0.44, 0, 0.012, 0.08, 0.1, tc)
    # 头部 (随俯仰)
    glPushMatrix()
    glTranslatef(0, 0.62, 0)
    glRotatef(lean_fwd * 0.7 + math.degrees(pitch) * 0.6, 1, 0, 0)
    draw_box(0, 0.06, 0, 0.2, 0.22, 0.22, SKIN)
    draw_box(0, 0.16, 0.0, 0.26, 0.12, 0.28, V)
    draw_box(0, 0.11, -0.12, 0.2, 0.05, 0.04, (0.1, 0.1, 0.1))  # 护目镜
    glPopMatrix()
    # 手臂与武器 (随俯仰)
    glPushMatrix()
    glTranslatef(0, 0.5, 0)
    aim = math.degrees(pitch) + lean_fwd
    if sprint:
        aim = -35 + lean_fwd * 0.5
    glRotatef(aim, 1, 0, 0)
    gp = (0.1, -0.12, -0.32)
    glPushMatrix()
    glTranslatef(*gp)
    if sprint:
        glRotatef(35, 0, 1, 0)
    _gun(gun_cache, w.id if w else None, w.atts if w else None)
    glPopMatrix()
    rhand = (gp[0], gp[1] - 0.04, gp[2] + 0.03)
    lz = -0.28 if (w and w.defn.category != "pistol") else 0.0
    lhand = (gp[0] - 0.03, gp[1] - 0.02, gp[2] + lz)
    for sh, hand, pole in (((0.22, 0.0, 0.0), rhand, (1, -1, 0.3)), ((-0.22, 0.0, 0.0), lhand, (-1, -1, 0.3))):
        el = _arm_ik(sh, hand, 0.28, 0.3, pole)
        draw_limb(sh, el, 0.1, 0.1, U)
        draw_limb(el, hand, 0.085, 0.085, U)
    glPopMatrix()
    glPopMatrix()
    glPopMatrix()


def draw_corpse(c, gun_cache):
    t = c["t"]
    fall = smoothstep(min(1.0, t / 0.55))
    U, V = UNIFORM[c["team"]], VEST[c["team"]]
    x, y, z = c["pos"]
    sink = max(0.0, t - 7.0) * 0.3
    glPushMatrix()
    glTranslatef(x, y - sink, z)
    glRotatef(-math.degrees(c["yaw"]), 0, 1, 0)
    if c["stance"] == PRONE:
        fall = 1.0
    glRotatef(88 * fall * c["side"] * 0 + 0, 0, 0, 1)
    glRotatef(-85 * fall, 1, 0, 0)
    hip = 0.95 if c["stance"] == STAND else 0.6
    glTranslatef(0, -hip * fall * 0.75 + 0.1 * fall, 0)
    for s in (-1, 1):
        draw_limb((s * 0.11, hip, 0), (s * 0.15, 0.0, 0.05), 0.15, 0.15, U)
    draw_box(0, hip + 0.28, 0, 0.4, 0.56, 0.24, U)
    draw_box(0, hip + 0.32, -0.01, 0.43, 0.4, 0.28, V)
    draw_box(0, hip + 0.68, 0, 0.2, 0.22, 0.22, SKIN)
    draw_box(0, hip + 0.78, 0, 0.26, 0.12, 0.28, V)
    for s in (-1, 1):
        draw_limb((s * 0.22, hip + 0.5, 0), (s * (0.35 + 0.2 * fall), hip + 0.6 + 0.2 * fall, -0.1), 0.09, 0.09, U)
    glPopMatrix()
