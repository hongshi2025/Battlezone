"""菜单: 主菜单 / 部署(选枪) / Plus 配件菜单 (Z) / 暂停"""
import math
from OpenGL.GL import *
from core import settings as S
from core.mathutil import clamp
from weapons.definitions import (WEAPONS, CATEGORY_NAMES, PRIMARY_CATEGORIES, weapons_in, secondary_weapons,
                                 PISTOL, RIFLE, SNIPER, SHOTGUN)
from weapons.attachments import (ATTACHMENTS, SLOTS, SLOT_NAMES, options_for, compute_stats, sanitize,
                                 default_attachments, DEFAULT_BY_SLOT)
from render.gl_util import rect, rect_outline, rect_grad, line, circle
from .widgets import (ACCENT, ACCENT_DIM, WHITE, GREY, ENEMY, FRIEND, GOLD, PANEL, DARK_TEXT,
                      draw_gun_preview)

DIFFICULTY = [("简单", 0.25), ("普通", 0.5), ("困难", 0.72), ("精英", 0.92)]


# ════════════════ 属性归一化 (用于属性条) ════════════════
def stat_values(wdef, st):
    return {
        "伤害": (clamp(st.damage_near * st.pellets / 110.0, 0, 1), "%.0f%s" % (st.damage_near, (" x%d" % st.pellets) if st.pellets > 1 else "")),
        "射速": (clamp(wdef.rpm / 1150.0, 0, 1), "%d RPM" % wdef.rpm),
        "射程": (clamp(st.range_far / 240.0, 0, 1), "%d m" % st.range_far),
        "后坐控制": (clamp(1 - st.recoil_v / 6.5, 0, 1), "%.2f°" % st.recoil_v),
        "腰射精度": (clamp(1 - st.spread_hip / 8.0, 0, 1), "%.1f°" % st.spread_hip),
        "开镜速度": (clamp(1 - (st.ads_time - 0.1) / 0.55, 0, 1), "%d ms" % (st.ads_time * 1000)),
        "机动性": (clamp(st.move_mult / 1.08, 0, 1), "%d%%" % (st.move_mult * 100)),
    }


def draw_stats(ui, x, y, w, wdef, atts):
    st = compute_stats(wdef, atts)
    base = compute_stats(wdef, default_attachments(wdef))
    sv, bv = stat_values(wdef, st), stat_values(wdef, base)
    for k, (v, txt) in sv.items():
        ui.stat_bar(x, y, w, k, v, v - bv[k][0], txt)
        y += 40
    ui.label("弹匣", x, y, 14, GREY, shadow=False)
    ui.label("%d / %d" % (st.mag_size, st.mag_size * wdef.reserve_mags), x + w, y, 14, WHITE, align="right",
             shadow=False)
    y += 22
    ui.label("换弹", x, y, 14, GREY, shadow=False)
    txt = ("%.1fs/发" % st.shell_time) if wdef.shell_reload else ("%.1fs / %.1fs" % (st.reload_time, st.reload_empty_time))
    ui.label(txt, x + w, y, 14, WHITE, align="right", shadow=False)
    y += 22
    ui.label("射击模式", x, y, 14, GREY, shadow=False)
    names = {"auto": "全自动", "semi": "半自动", "burst": "%d连发" % wdef.burst_count, "bolt": "手动", "pump": "泵动"}
    ui.label(" / ".join(names[m] for m in wdef.fire_modes), x + w, y, 14, WHITE, align="right", shadow=False)
    return y + 22


# ════════════════ Plus 配件菜单 ════════════════
class PlusMenu:
    """BF2042 风格: 四个方向对应四个配件槽, 中间显示武器"""

    def __init__(self, ui, gun_cache):
        self.ui = ui
        self.cache = gun_cache
        self.is_open = False
        self.wid = None
        self.atts = None
        self.on_change = None
        self.hover_att = None
        self.t = 0.0
        self.tabs = None          # [(label, wid, atts, callback)]
        self.tab_idx = 0
        self.in_game = False

    def open(self, tabs, tab_idx=0, in_game=False):
        self.tabs = tabs
        self.tab_idx = tab_idx
        self.is_open = True
        self.in_game = in_game
        self._select_tab(tab_idx)

    def _select_tab(self, i):
        self.tab_idx = i
        _, self.wid, atts, self.on_change = self.tabs[i]
        self.atts = dict(atts)

    def close(self):
        self.is_open = False

    def draw(self, dt):
        if not self.is_open:
            return
        self.t += dt
        ui = self.ui
        W, H = ui.w, ui.h
        cx, cy = W / 2, H / 2 + 10
        wdef = WEAPONS[self.wid]
        rect(0, 0, W, H, (0.01, 0.02, 0.03), 0.55 if self.in_game else 0.75)
        rect_grad(0, cy - 110, W, 220, (0.1, 0.4, 0.5), (0.1, 0.4, 0.5), 0.0, 0.0)
        # 标题 + 标签页
        ui.label("配件  ·  " + wdef.name, cx, 26, 22, WHITE, align="center", bold=True)
        ui.label("按 Z / Esc 关闭    点击选择配件" + ("    (更换配件时无法射击)" if self.in_game else ""),
                 cx, 56, 13, GREY, align="center")
        if len(self.tabs) > 1:
            tw = 160
            x0 = cx - tw * len(self.tabs) / 2
            for i, (label, wid, _, _) in enumerate(self.tabs):
                if ui.button(x0 + i * tw, 80, tw - 6, 30, label + " · " + WEAPONS[wid].name, i == self.tab_idx, size=13):
                    self._select_tab(i)
                    wdef = WEAPONS[self.wid]
        # 中央武器预览
        pw, ph = 460, 200
        model = self.cache.get(self.wid, self.atts)
        rect_outline(cx - pw / 2, cy - ph / 2, pw, ph, ACCENT, 0.25)
        ui.corner_frame(cx - pw / 2, cy - ph / 2, pw, ph)
        draw_gun_preview(model, cx - pw / 2, cy - ph / 2, pw, ph, W, H, self.t, spin=False,
                         yaw=90 + math.sin(self.t * 0.6) * 12)
        glEnable(GL_BLEND)
        self.hover_att = None
        bw, bh = 176, 38
        # 上: 瞄具
        self._slot_row("optic", cx, cy - ph / 2 - 20 - bh, bw, bh, horizontal=True, label_above=True)
        # 下: 弹匣
        self._slot_row("magazine", cx, cy + ph / 2 + 24, bw, bh, horizontal=True, label_above=False)
        # 左: 枪口
        self._slot_row("muzzle", cx - pw / 2 - 30 - bw, cy, bw, bh, horizontal=False, label_above=True)
        # 右: 下挂
        self._slot_row("underbarrel", cx + pw / 2 + 30, cy, bw, bh, horizontal=False, label_above=True)
        # 说明 & 属性
        if self.hover_att:
            a = ATTACHMENTS[self.hover_att]
            ui.label(a.name + " — " + a.desc, cx, H - 150, 15, WHITE, align="center")
        draw_stats_compact(ui, W - 250, 130, 220, wdef, self.atts)

    def _slot_row(self, slot, x, y, bw, bh, horizontal, label_above):
        ui = self.ui
        wdef = WEAPONS[self.wid]
        opts = options_for(wdef, slot)
        n = len(opts)
        arrow = {"optic": "▲", "magazine": "▼", "muzzle": "◀", "underbarrel": "▶"}[slot]
        if horizontal:
            total = n * (bw + 6) - 6
            x0 = x - total / 2
            ly = y - 22 if label_above else y + bh + 6
            ui.label(arrow + " " + SLOT_NAMES[slot], x, ly, 14, ACCENT, align="center", bold=True)
            for i, a in enumerate(opts):
                self._opt(slot, a, x0 + i * (bw + 6), y, bw, bh)
        else:
            total = n * (bh + 6) - 6
            y0 = y - total / 2
            ui.label(arrow + " " + SLOT_NAMES[slot], x + bw / 2, y0 - 24, 14, ACCENT, align="center", bold=True)
            for i, a in enumerate(opts):
                self._opt(slot, a, x, y0 + i * (bh + 6), bw, bh)

    def _opt(self, slot, a, x, y, w, h):
        ui = self.ui
        sel = self.atts.get(slot) == a.id
        if ui.hover(x, y, w, h):
            self.hover_att = a.id
        if ui.button(x, y, w, h, a.name, selected=sel, size=14):
            if not sel:
                self.atts[slot] = a.id
                self.atts = sanitize(WEAPONS[self.wid], self.atts)
                label, wid, _, cb = self.tabs[self.tab_idx]
                self.tabs[self.tab_idx] = (label, wid, dict(self.atts), cb)
                if cb:
                    cb(dict(self.atts))


def draw_stats_compact(ui, x, y, w, wdef, atts):
    ui.panel(x - 12, y - 12, w + 24, 350, 0.6)
    ui.label("属性", x, y, 14, ACCENT, bold=True)
    draw_stats(ui, x, y + 26, w, wdef, atts)


# ════════════════ 部署界面 (每次重生选择主/副武器) ════════════════
class LoadoutScreen:
    def __init__(self, ui, gun_cache, prefs):
        self.ui = ui
        self.cache = gun_cache
        self.prefs = prefs
        self.t = 0.0
        p = WEAPONS.get(prefs.data.get("primary", "m4a1"))
        self.category = p.category if p else RIFLE
        self.focus = "primary"

    def atts_for(self, wid):
        saved = self.prefs.data.setdefault("atts", {}).get(wid)
        return sanitize(WEAPONS[wid], saved) if saved else default_attachments(WEAPONS[wid])

    def set_atts(self, wid, atts):
        self.prefs.data.setdefault("atts", {})[wid] = dict(atts)
        self.prefs.save()

    def loadout(self):
        d = self.prefs.data
        prim = d.get("primary", "m4a1")
        sec = d.get("secondary", "p320")
        return dict(primary=prim, secondary=sec, primary_atts=self.atts_for(prim), secondary_atts=self.atts_for(sec))

    def draw(self, dt, respawn_t, team, plus_menu, status=""):
        """返回 'deploy' / 'menu' / None"""
        self.t += dt
        ui = self.ui
        W, H = ui.w, ui.h
        d = self.prefs.data
        rect(0, 0, W, H, (0.02, 0.03, 0.05), 0.72)
        rect_grad(0, 0, W, 70, (0, 0, 0), (0, 0, 0), 0.7, 0.0)
        ui.label("部署", 30, 18, 30, WHITE, bold=True)
        ui.label("DEPLOY  ·  选择你的武器配置", 110, 30, 14, GREY)
        ui.label(S.TEAM_NAMES[team], W - 30, 22, 18, FRIEND if team == 0 else ENEMY, align="right", bold=True)
        if status:
            ui.label(status, W - 30, 48, 12, GREY, align="right")
        result = None

        # ── 左侧: 主武器 ──
        x, y = 30, 90
        ui.label("主武器", x, y, 16, ACCENT, bold=True)
        y += 28
        tw = 106
        for i, cat in enumerate(PRIMARY_CATEGORIES):
            if ui.button(x + i * (tw + 6), y, tw, 32, CATEGORY_NAMES[cat], cat == self.category, size=15):
                self.category = cat
        y += 42
        for wdef in weapons_in(self.category):
            sel = d.get("primary") == wdef.id
            if ui.button(x, y, 330, 48, wdef.name, sel, size=16, sub=wdef.caliber, align="left"):
                d["primary"] = wdef.id
                self.focus = "primary"
                self.prefs.save()
            y += 54
        # 副武器
        y = max(y + 10, 440)
        ui.label("副武器  ·  手枪", x, y, 16, ACCENT, bold=True)
        y += 28
        for i, wdef in enumerate(secondary_weapons()):
            sel = d.get("secondary") == wdef.id
            bx = x + (i % 2) * 168
            by = y + (i // 2) * 50
            if ui.button(bx, by, 162, 44, wdef.name, sel, size=14, sub=wdef.caliber):
                d["secondary"] = wdef.id
                self.focus = "secondary"
                self.prefs.save()

        # ── 中间: 预览 ──
        fid = d.get(self.focus, "m4a1")
        wdef = WEAPONS[fid]
        atts = self.atts_for(fid)
        px, py, pw, ph = 390, 90, 500, 260
        if ui.button(px, py, 120, 28, "主武器", self.focus == "primary", size=13):
            self.focus = "primary"
        if ui.button(px + 126, py, 120, 28, "副武器", self.focus == "secondary", size=13):
            self.focus = "secondary"
        py += 36
        ui.panel(px, py, pw, ph, 0.35)
        ui.corner_frame(px, py, pw, ph)
        model = self.cache.get(fid, atts)
        draw_gun_preview(model, px, py, pw, ph, W, H, self.t)
        glEnable(GL_BLEND)
        ui.label(wdef.name, px + 14, py + 10, 22, WHITE, bold=True)
        ui.label(CATEGORY_NAMES[wdef.category] + "  ·  " + wdef.caliber, px + 14, py + 40, 13, GREY)
        ui.label(wdef.real, px, py + ph + 10, 13, GREY)
        # 配件
        ay = py + ph + 40
        ui.label("配件 (按 Z 或点击自定义)", px, ay, 14, ACCENT, bold=True)
        ay += 24
        sw = (pw - 18) / 4
        for i, slot in enumerate(SLOTS):
            a = ATTACHMENTS[atts[slot]]
            if ui.button(px + i * (sw + 6), ay, sw, 50, a.name, False, size=14, sub=SLOT_NAMES[slot]):
                self.open_plus(plus_menu)
        # ── 右侧: 属性 ──
        sx = 920
        ui.panel(sx - 14, 90, W - sx - 16, 420, 0.5)
        ui.label("武器属性", sx, 102, 16, ACCENT, bold=True)
        draw_stats(ui, sx, 132, W - sx - 44, wdef, atts)

        # ── 底部: 部署按钮 ──
        prim, sec = WEAPONS[d.get("primary", "m4a1")], WEAPONS[d.get("secondary", "p320")]
        ui.label("当前配置:  %s  +  %s" % (prim.name, sec.name), px, H - 60, 15, WHITE)
        ready = respawn_t <= 0
        label = "部署" if ready else "部署  %.1f" % respawn_t
        if ui.button(W - 350, H - 90, 320, 60, label, selected=ready, enabled=ready, size=24,
                     sub="ENTER / 空格" if ready else "重生冷却中"):
            result = "deploy"
        if ui.button(30, H - 70, 150, 40, "返回主菜单", size=14):
            result = "menu"
        return result

    def open_plus(self, plus_menu):
        d = self.prefs.data
        tabs = []
        for label, key in (("主武器", "primary"), ("副武器", "secondary")):
            wid = d.get(key)
            tabs.append((label, wid, self.atts_for(wid), (lambda w: (lambda a: self.set_atts(w, a)))(wid)))
        plus_menu.open(tabs, 0 if self.focus == "primary" else 1)


# ════════════════ 主菜单 ════════════════
class MainMenu:
    def __init__(self, ui, prefs):
        self.ui = ui
        self.prefs = prefs
        self.t = 0.0
        self.page = "main"
        self.focus_field = None

    def text_input(self, ch):
        if self.focus_field:
            v = self.prefs.data.get(self.focus_field, "")
            if len(v) < 32:
                self.prefs.data[self.focus_field] = v + ch

    def backspace(self):
        if self.focus_field:
            self.prefs.data[self.focus_field] = self.prefs.data.get(self.focus_field, "")[:-1]

    def draw(self, dt, info=""):
        """返回 ('single',) / ('connect', host, port) / ('quit',) / None"""
        self.t += dt
        ui = self.ui
        W, H = ui.w, ui.h
        d = self.prefs.data
        rect_grad(0, 0, W * 0.6, H, (0.01, 0.02, 0.03), (0.01, 0.02, 0.03), 0.92, 0.0, horizontal=True)
        ui.label("PORTAL STRIKE", 70, 70, 54, WHITE, bold=True)
        ui.label("2042", 70 + ui.text.size("PORTAL STRIKE", 54, True)[0] + 16, 70, 54, ACCENT, bold=True)
        ui.label("Python 第一人称射击  ·  战地风格原型  ·  v" + S.GAME_VERSION, 72, 138, 15, GREY)
        rect(72, 166, 90, 3, ACCENT, 1)
        res = None
        x, y = 70, 210
        if self.page == "main":
            if ui.button(x, y, 320, 56, "单人对战", size=22, sub="团队死斗 · 对抗 AI 机器人", align="left"):
                self.page = "single"
            y += 66
            if ui.button(x, y, 320, 56, "多人游戏", size=22, sub="连接专用服务器", align="left"):
                self.page = "multi"
            y += 66
            if ui.button(x, y, 320, 56, "操作说明", size=22, sub="动作系统 / 按键", align="left"):
                self.page = "help"
            y += 66
            if ui.button(x, y, 320, 56, "退出游戏", size=22, align="left"):
                res = ("quit",)
        elif self.page == "single":
            ui.label("单人对战设置", x, y, 20, WHITE, bold=True)
            y += 44
            y = self._stepper(x, y, "友军机器人", "friendly_bots", 0, 10)
            y = self._stepper(x, y, "敌军机器人", "enemy_bots", 1, 12)
            ui.label("难度", x, y + 10, 16, GREY)
            for i, (name, _) in enumerate(DIFFICULTY):
                if ui.button(x + 150 + i * 86, y, 80, 36, name, d.get("difficulty", 1) == i, size=14):
                    d["difficulty"] = i
                    self.prefs.save()
            y += 50
            y = self._stepper(x, y, "鼠标灵敏度", "sens", 0.2, 3.0, step=0.1, fmt="%.1f")
            y += 16
            if ui.button(x, y, 320, 56, "开始游戏", selected=True, size=22):
                res = ("single",)
            if ui.button(x + 330, y, 140, 56, "返回", size=18):
                self.page = "main"
        elif self.page == "multi":
            ui.label("连接到专用服务器", x, y, 20, WHITE, bold=True)
            y += 44
            y = self._field(x, y, "玩家名", "name")
            y = self._field(x, y, "服务器地址", "server")
            ui.label("启动服务器:  cd fps_game  &&  python -m net.server --port %d --bots 3" % S.DEFAULT_PORT,
                     x, y + 4, 13, GREY)
            y += 36
            if ui.button(x, y, 320, 56, "连接", selected=True, size=22):
                addr = d.get("server", "127.0.0.1:%d" % S.DEFAULT_PORT)
                host, _, port = addr.partition(":")
                try:
                    res = ("connect", host.strip() or "127.0.0.1", int(port or S.DEFAULT_PORT))
                except ValueError:
                    res = None
            if ui.button(x + 330, y, 140, 56, "返回", size=18):
                self.page = "main"
                self.focus_field = None
        elif self.page == "help":
            self._help(x, y)
            if ui.button(x, H - 90, 140, 48, "返回", size=18):
                self.page = "main"
        if info:
            ui.label(info, x, H - 36, 14, GOLD)
        return res

    def _stepper(self, x, y, label, key, lo, hi, step=1, fmt="%d"):
        ui = self.ui
        d = self.prefs.data
        v = d.get(key, lo)
        ui.label(label, x, y + 10, 16, GREY)
        if ui.button(x + 150, y, 40, 36, "−", size=18):
            d[key] = max(lo, round(v - step, 2))
            self.prefs.save()
        ui.label(fmt % d.get(key, v), x + 240, y + 18, 18, WHITE, align="center", valign="middle", bold=True)
        if ui.button(x + 290, y, 40, 36, "+", size=18):
            d[key] = min(hi, round(v + step, 2))
            self.prefs.save()
        return y + 48

    def _field(self, x, y, label, key):
        ui = self.ui
        ui.label(label, x, y + 10, 16, GREY)
        fx, fw = x + 150, 300
        focused = self.focus_field == key
        rect(fx, y, fw, 36, (0.05, 0.07, 0.1), 0.9)
        rect_outline(fx, y, fw, 36, ACCENT if focused else (1, 1, 1), 0.8 if focused else 0.15)
        txt = self.prefs.data.get(key, "")
        caret = "|" if focused and int(self.t * 2) % 2 == 0 else ""
        ui.label(txt + caret, fx + 10, y + 18, 16, WHITE, valign="middle")
        if ui.clicked:
            if ui.hover(fx, y, fw, 36):
                self.focus_field = key
            elif self.focus_field == key:
                self.focus_field = None
                self.prefs.save()
        return y + 48

    def _help(self, x, y):
        ui = self.ui
        rows = [
            ("移动", "W A S D"), ("冲刺", "按住 Shift (冲刺时 FOV 增大, 枪械下压为战术冲刺姿态)"),
            ("跳跃 / 翻越", "空格 — 面对 0.45~1.65m 的障碍自动翻越 (空中贴墙亦可)"),
            ("半蹲", "C 或 左Ctrl (切换)"), ("滑铲", "冲刺中按 C — 保持动量, 可接跳跃 (滑铲跳)"),
            ("趴下", "X 或 长按 C — 趴下/起身有过渡动作, 匍匐时不能射击"),
            ("侧身", "Q / E (探头射击)"), ("瞄准 / 射击", "右键 / 左键"),
            ("换弹", "R — 战术换弹与空仓换弹不同动作; 霰弹逐发装填可被射击打断"),
            ("切换武器", "1 / 2 / 鼠标滚轮"), ("射击模式", "B"), ("检视武器", "T"),
            ("配件菜单", "Z — 游戏中实时改装 (BF2042 Plus 系统)"),
            ("计分板 / 暂停", "Tab / Esc"),
        ]
        for k, v in rows:
            ui.label(k, x, y, 16, ACCENT, bold=True)
            ui.label(v, x + 150, y, 15, WHITE)
            y += 30


# ════════════════ 暂停 ════════════════
class PauseMenu:
    def __init__(self, ui, prefs):
        self.ui = ui
        self.prefs = prefs

    def draw(self):
        ui = self.ui
        W, H = ui.w, ui.h
        rect(0, 0, W, H, (0, 0, 0), 0.6)
        x, y = W / 2 - 160, H / 2 - 170
        ui.label("暂停", W / 2, y - 50, 32, WHITE, align="center", bold=True)
        res = None
        if ui.button(x, y, 320, 50, "继续游戏", selected=True, size=20):
            res = "resume"
        y += 60
        if ui.button(x, y, 320, 50, "重新部署 (更换武器)", size=18):
            res = "redeploy"
        y += 60
        d = self.prefs.data
        ui.label("灵敏度  %.1f" % d.get("sens", 1.0), W / 2, y + 25, 17, WHITE, align="center", valign="middle")
        if ui.button(x, y, 50, 50, "−", size=20):
            d["sens"] = max(0.2, round(d.get("sens", 1.0) - 0.1, 2))
        if ui.button(x + 270, y, 50, 50, "+", size=20):
            d["sens"] = min(3.0, round(d.get("sens", 1.0) + 0.1, 2))
        y += 60
        fov = d.get("fov", S.FOV_BASE)
        ui.label("视野 FOV  %d" % fov, W / 2, y + 25, 17, WHITE, align="center", valign="middle")
        if ui.button(x, y, 50, 50, "−", size=20):
            d["fov"] = max(60, fov - 2)
        if ui.button(x + 270, y, 50, 50, "+", size=20):
            d["fov"] = min(100, fov + 2)
        y += 60
        if ui.button(x, y, 320, 50, "返回主菜单", size=18):
            res = "menu"
        return res
