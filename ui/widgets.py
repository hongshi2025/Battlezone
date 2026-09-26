"""简易即时模式 UI (BF2042 风格: 深色半透明面板 + 青色强调)"""
import math
from OpenGL.GL import *
from render.gl_util import rect, rect_outline, rect_grad, line, circle, TextRenderer, perspective

BG = (0.04, 0.06, 0.08)
PANEL = (0.07, 0.09, 0.12)
ACCENT = (0.25, 0.85, 1.0)
ACCENT_DIM = (0.12, 0.42, 0.52)
WHITE = (1.0, 1.0, 1.0)
GREY = (0.62, 0.66, 0.70)
DARK_TEXT = (0.05, 0.07, 0.09)
ENEMY = (1.0, 0.34, 0.24)
FRIEND = (0.35, 0.72, 1.0)
GOLD = (1.0, 0.78, 0.25)


def c255(c):
    return (int(c[0] * 255), int(c[1] * 255), int(c[2] * 255))


class UI:
    def __init__(self, width, height):
        self.w = width
        self.h = height
        self.text = TextRenderer()
        self.mx = 0
        self.my = 0
        self.clicked = False
        self.right_clicked = False
        self.mouse_down = False
        self.hot = None
        self.sound_cb = None

    def begin_frame(self, mx, my, clicked, right_clicked=False):
        self.mx, self.my = mx, my
        self.clicked = clicked
        self.right_clicked = right_clicked

    def hover(self, x, y, w, h):
        return x <= self.mx <= x + w and y <= self.my <= y + h

    def label(self, s, x, y, size=18, color=WHITE, alpha=1.0, align="left", bold=False, valign="top", shadow=True):
        return self.text.draw(s, x, y, size, c255(color), alpha, align, bold, shadow, valign)

    def panel(self, x, y, w, h, alpha=0.78, color=PANEL, border=None):
        rect(x, y, w, h, color, alpha)
        if border:
            rect_outline(x, y, w, h, border, 0.6)

    def button(self, x, y, w, h, text, selected=False, enabled=True, size=18, sub=None, align="center",
               accent=ACCENT):
        hov = enabled and self.hover(x, y, w, h)
        if selected:
            rect(x, y, w, h, accent, 0.92)
            tc = DARK_TEXT
        else:
            rect(x, y, w, h, PANEL, 0.82 if hov else 0.68)
            tc = WHITE if enabled else GREY
        if hov and not selected:
            rect(x, y + h - 3, w, 3, accent, 1.0)
            rect_outline(x, y, w, h, accent, 0.7)
        elif not selected:
            rect_outline(x, y, w, h, (1, 1, 1), 0.08)
        if sub:
            ty = y + h * 0.36
            tx = x + w / 2 if align == "center" else x + 12
            self.label(text, tx, ty, size, tc, align=align, valign="middle", bold=True, shadow=not selected)
            self.label(sub, tx, y + h * 0.72, max(11, size - 6), tc if selected else GREY, align=align,
                       valign="middle", shadow=False)
        else:
            tx = x + w / 2 if align == "center" else x + 12
            self.label(text, tx, y + h / 2, size, tc, align=align, valign="middle", bold=True, shadow=not selected)
        if hov and self.clicked:
            if self.sound_cb:
                self.sound_cb()
            return True
        return False

    def stat_bar(self, x, y, w, label, value, delta=0.0, text=None):
        """value/delta ∈ [0,1]"""
        self.label(label, x, y, 14, GREY, shadow=False)
        if text:
            self.label(text, x + w, y, 14, WHITE, align="right", shadow=False)
        by = y + 20
        rect(x, by, w, 6, (1, 1, 1), 0.12)
        base = max(0.0, min(1.0, value - max(0.0, delta)))
        rect(x, by, w * base, 6, WHITE, 0.9)
        if delta > 0:
            rect(x + w * base, by, w * min(delta, 1 - base), 6, (0.3, 1.0, 0.45), 0.95)
        elif delta < 0:
            v = max(0.0, min(1.0, value))
            rect(x + w * v, by, w * min(-delta, 1 - v), 6, (1.0, 0.35, 0.3), 0.95)

    def corner_frame(self, x, y, w, h, color=ACCENT, l=14, alpha=0.9):
        glLineWidth(2)
        glColor4f(*color, alpha)
        glBegin(GL_LINES)
        for cx, cy, dx, dy in ((x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
            glVertex2f(cx, cy); glVertex2f(cx + dx * l, cy)
            glVertex2f(cx, cy); glVertex2f(cx, cy + dy * l)
        glEnd()


def draw_gun_preview(model, x, y, w, h, screen_w, screen_h, t, spin=True, yaw=None):
    """在屏幕区域内渲染 3D 枪械预览"""
    rect_grad(x, y, w, h, (0.32, 0.36, 0.40), (0.10, 0.12, 0.14), 0.55, 0.55)
    glViewport(int(x), int(screen_h - y - h), int(w), int(h))
    glMatrixMode(GL_PROJECTION)
    glPushMatrix()
    glLoadIdentity()
    perspective(30, w / float(h), 0.05, 20)
    glMatrixMode(GL_MODELVIEW)
    glPushMatrix()
    glLoadIdentity()
    glClear(GL_DEPTH_BUFFER_BIT)
    glEnable(GL_DEPTH_TEST)
    glEnable(GL_LIGHTING)
    glEnable(GL_LIGHT0)
    glLightfv(GL_LIGHT0, GL_POSITION, (0.3, 0.8, 0.6, 0.0))
    glLightfv(GL_LIGHT0, GL_DIFFUSE, (1.0, 1.0, 1.0, 1))
    glLightfv(GL_LIGHT0, GL_AMBIENT, (0.75, 0.77, 0.8, 1))
    glEnable(GL_COLOR_MATERIAL)
    glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
    glEnable(GL_NORMALIZE)
    glDisable(GL_FOG)
    glDisable(GL_TEXTURE_2D)
    glDisable(GL_BLEND)
    L = max(0.3, model.length)
    glTranslatef(0, -0.01, -L * 1.75)
    glRotatef(8, 1, 0, 0)
    ang = yaw if yaw is not None else (90 + math.sin(t * 0.5) * 25 if spin else 90)
    glRotatef(ang, 0, 1, 0)
    center_z = 0.33 - L / 2
    glTranslatef(0, 0, -center_z)
    for g in ("body", "mag", "bolt", "slide", "pump"):
        model.draw(g)
    glDisable(GL_LIGHTING)
    glEnable(GL_BLEND)
    glPopMatrix()
    glMatrixMode(GL_PROJECTION)
    glPopMatrix()
    glMatrixMode(GL_MODELVIEW)
    glViewport(0, 0, int(screen_w), int(screen_h))
    glDisable(GL_DEPTH_TEST)
