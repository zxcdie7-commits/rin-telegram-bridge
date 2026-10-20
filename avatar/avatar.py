"""Рин на экране: окошко поверх всех окон, живое — дышит, покачивается, моргает, говорит.

Слои (art/, делает avatar_art.py из листа персонажа владельца):
  base_<эмоция>.png   лицо: idle, smile, laugh, sad, angry, surprised
  mouth_<эмоция>.png  открытый рот поверх лица — прозрачность по громкости голоса
  blink_<эмоция>.png  закрытые глаза поверх лица
Голос на Маке и звонки Telegram шлют по UDP 127.0.0.1:3014: «L 0.31» громкость, «T laughs,softly» теги реплики,
«E» конец речи. Эмоция реплики держится HOLD_S, потом лицо плавно возвращается к фоновому: в диалоге — тёплая
полуулыбка, иначе — по её настроению дня из дневника; пока думает — голова чуть наклонена.
Окно перетаскивается мышью; двойной клик — спрятать (вернуть — «Рин на экране» в значке меню).
"""

import json
import math
import random
import re
import socket
import time
from pathlib import Path

import AppKit
import objc
import Quartz
from Foundation import NSObject, NSTimer

BASE = Path(__file__).resolve().parent
ART = BASE / "art"
POS_FILE = BASE / "avatar_pos.json"
STATUS_FILE = BASE / "status.json"
DIARY = Path.home() / ".hermes" / "memories" / "RIN_DIARY.md"
PORT = 3014
WIDTH = 260
HOLD_S = 25          # эмоция реплики держится после неё
FADE_S = 0.35        # переход между эмоциями
MOUTH_FULL = 0.12    # громкость, при которой рот открыт полностью
MOODS = {   # тег голоса → эмоция; первое совпадение по порядку
    "laugh": {"laughs", "giggles", "excited", "happy", "laughing"},
    "smile": {"chuckles", "warmly", "playfully", "teasing", "smirks", "softly", "shy", "flirty", "whispers"},
    "sad": {"sighs", "tired", "sad", "hesitant", "apologetic", "crying"},
    "angry": {"annoyed", "angry", "frustrated", "sarcastic", "deadpan"},
    "surprised": {"surprised", "gasps", "curious", "shocked"},
}
GOOD_DAY = re.compile(r"(хорош|рад|весел|тепл|уютн|спокойн|солн|улыб)", re.I)


def image(name):
    path = ART / f"{name}.png"
    if not path.exists():
        return None
    img = AppKit.NSImage.alloc().initWithContentsOfFile_(str(path))
    return img.CGImageForProposedRect_context_hints_(None, None, None)[0]


def resting_mood():
    """Фон лица без реплики: в разговоре — полуулыбка, иначе по её настроению дня."""
    try:
        mode = json.loads(STATUS_FILE.read_text()).get("mode", "")
    except (OSError, ValueError):
        mode = ""
    if mode in ("dialog", "speaking"):
        return "smile", mode
    try:
        m = re.search(r"^Настроение:\s*(.+)$", DIARY.read_text(), re.M)
        day = m.group(1) if m else ""
    except OSError:
        day = ""
    return ("smile" if GOOD_DAY.search(day) else "idle"), mode


def layer(frame, anchor=(0.5, 0.0)):
    l = Quartz.CALayer.layer()
    l.setAnchorPoint_(anchor)
    l.setFrame_(frame)
    l.setContentsGravity_(Quartz.kCAGravityResizeAspect)
    return l


def forever(key, from_, to, duration, phase=0.0):
    a = Quartz.CABasicAnimation.animationWithKeyPath_(key)
    a.setFromValue_(from_)
    a.setToValue_(to)
    a.setDuration_(duration)
    a.setAutoreverses_(True)
    a.setRepeatCount_(1e9)
    a.setTimeOffset_(phase)
    a.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseInEaseOut))
    return a


class DragView(AppKit.NSView):
    def mouseDownCanMoveWindow(self):
        return True

    def mouseDown_(self, event):
        if event.clickCount() == 2:
            AppKit.NSApp.terminate_(None)
            return
        self.window().performWindowDragWithEvent_(event)

    def mouseUp_(self, event):
        x, y = self.window().frame().origin
        try:
            POS_FILE.write_text(json.dumps({"x": x, "y": y}))
        except OSError:
            pass


class Avatar(NSObject):
    def init(self):
        self = objc.super(Avatar, self).init()
        self.images = {}
        for mood in list(MOODS) + ["idle"]:
            self.images[mood] = (image(f"base_{mood}"), image(f"mouth_{mood}"), image(f"blink_{mood}"))
        w0 = Quartz.CGImageGetWidth(self.images["idle"][0])
        h0 = Quartz.CGImageGetHeight(self.images["idle"][0])
        height = WIDTH * h0 / w0
        pad = 12   # запас по краям, чтобы покачивание не обрезалось

        screen = AppKit.NSScreen.mainScreen().visibleFrame()
        try:
            pos = json.loads(POS_FILE.read_text())
            x, y = pos["x"], pos["y"]
        except (OSError, ValueError, KeyError):
            x, y = screen.origin.x + screen.size.width - WIDTH - 2 * pad - 16, screen.origin.y + 8
        size = (WIDTH + 2 * pad, height + 2 * pad)
        self.window = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((x, y), size), AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.window.setHasShadow_(False)
        self.window.setLevel_(AppKit.NSFloatingWindowLevel)
        self.window.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                           | AppKit.NSWindowCollectionBehaviorStationary)
        view = DragView.alloc().initWithFrame_(((0, 0), size))
        view.setWantsLayer_(True)
        self.window.setContentView_(view)

        # покачивание (низ — точка опоры) → наклон «думает» → дыхание → подпрыгивание в речи → слои лица
        frame = ((pad, pad), (WIDTH, height))
        self.sway = layer(frame)
        self.tilt = layer(((0, 0), (WIDTH, height)))
        self.breath = layer(((0, 0), (WIDTH, height)))
        self.bob = layer(((0, 0), (WIDTH, height)), (0.5, 0.5))
        self.face = layer(((0, 0), (WIDTH, height)), (0.5, 0.5))
        self.mouth = layer(((0, 0), (WIDTH, height)), (0.5, 0.5))
        self.blink = layer(((0, 0), (WIDTH, height)), (0.5, 0.5))
        view.layer().addSublayer_(self.sway)
        self.sway.addSublayer_(self.tilt)
        self.tilt.addSublayer_(self.breath)
        self.breath.addSublayer_(self.bob)
        for l in (self.face, self.mouth, self.blink):
            self.bob.addSublayer_(l)
        self.mouth.setOpacity_(0)
        self.blink.setOpacity_(0)
        self.sway.addAnimation_forKey_(forever("transform.rotation.z", -0.012, 0.012, 5.5), "sway")
        self.breath.addAnimation_forKey_(forever("transform.scale.y", 1.0, 1.014, 3.4), "breath")
        self.breath.addAnimation_forKey_(forever("transform.scale.x", 1.0, 1.004, 3.4), "breath-x")
        self.window.orderFrontRegardless()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", PORT))
        self.sock.setblocking(False)
        self.target, self.open = 0.0, 0.0
        self.speech_mood, self.speech_until, self.talk_until = "", 0.0, 0.0
        self.mood, self.thinking = None, False
        self.rest, self.mode, self.rest_checked = "idle", "", 0.0
        self.blink_at = time.time() + random.uniform(2, 5)
        self.set_mood("idle")
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1 / 60, self, "tick:", None, True)
        return self

    def set_mood(self, mood):
        if mood == self.mood or mood not in self.images or self.images[mood][0] is None:
            return
        fade = Quartz.CATransition.animation()
        fade.setType_(Quartz.kCATransitionFade)
        fade.setDuration_(FADE_S)
        for l in (self.face, self.mouth, self.blink):
            l.addAnimation_forKey_(fade, "contents")
        base, mouth, blink = self.images[mood]
        self.face.setContents_(base)
        self.mouth.setContents_(mouth)
        self.blink.setContents_(blink)
        self.mood = mood

    def do_blink(self):
        if self.images[self.mood][2] is None:
            return
        a = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        twice = random.random() < 0.15
        a.setValues_([0, 1, 1, 0, 0, 1, 1, 0] if twice else [0, 1, 1, 0])
        a.setKeyTimes_([0, 0.12, 0.38, 0.5, 0.62, 0.74, 0.88, 1] if twice else [0, 0.3, 0.6, 1])
        a.setDuration_(0.42 if twice else 0.2)
        self.blink.addAnimation_forKey_(a, "blink")

    def set_thinking(self, on):
        if on == self.thinking:
            return
        self.thinking = on
        a = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
        a.setFromValue_(self.tilt.presentationLayer().valueForKeyPath_("transform.rotation.z") if self.tilt.presentationLayer() else 0)
        a.setToValue_(0.06 if on else 0.0)
        a.setDuration_(0.6)
        a.setFillMode_(Quartz.kCAFillModeForwards)
        a.setRemovedOnCompletion_(False)
        a.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseInEaseOut))
        self.tilt.addAnimation_forKey_(a, "think")

    def tick_(self, _):
        now = time.time()
        while True:
            try:
                msg = self.sock.recv(256).decode(errors="ignore")
            except BlockingIOError:
                break
            if msg.startswith("L "):
                try:
                    self.target = max(self.target, float(msg[2:]))
                except ValueError:
                    pass
                self.talk_until = now + 0.25
                self.speech_until = now + HOLD_S
            elif msg.startswith("T "):
                tags = {t.strip() for t in msg[2:].split(",") if t.strip()}
                self.speech_mood = next((m for m, words in MOODS.items() if tags & words), "")
                self.speech_until = now + HOLD_S
            elif msg == "E":
                self.target, self.speech_until = 0.0, now + HOLD_S

        if now - self.rest_checked > 1:
            self.rest, self.mode = resting_mood()
            self.rest_checked = now
        self.set_thinking(self.mode == "thinking")
        self.set_mood(self.speech_mood if self.speech_mood and now < self.speech_until else self.rest)

        # рот: быстро открывается, мягче закрывается — как слоги
        if now > self.talk_until:
            self.target = 0.0
        goal = min(1.0, self.target / MOUTH_FULL)
        self.open += (goal - self.open) * (0.55 if goal > self.open else 0.22)
        self.target *= 0.7
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.mouth.setOpacity_(self.open if self.images[self.mood][1] is not None else 0)
        self.bob.setAffineTransform_(Quartz.CGAffineTransformMakeTranslation(0, self.open * 3.0))
        Quartz.CATransaction.commit()

        if now >= self.blink_at:
            self.blink_at = now + random.uniform(2.5, 6.5)
            self.do_blink()


if __name__ == "__main__":
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # без значка в Dock
    avatar = Avatar.alloc().init()
    app.run()
