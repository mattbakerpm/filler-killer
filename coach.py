#!/usr/bin/env python3
"""
Filler Coach — a local, real-time filler-word counter.

Listens to your microphone with an offline speech-to-text engine (Vosk),
counts filler words/phrases as you speak, and shows an always-on-top floating
overlay (native Cocoa panel — hovers even over fullscreen call windows).

No cloud, no API keys, no LLM/AI tokens — audio never leaves the Mac.

Usage:
    python coach.py                 # run the overlay
    python coach.py --echo          # also print recognized text (for tuning)
    python coach.py --dock          # show a Dock icon (used by FillerCoach.app)
    python coach.py --list-devices  # print available input devices and exit
"""

import argparse
import json
import os
import queue
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
MODEL_DIR = os.path.join(HERE, "model")
SAMPLE_RATE = 16000


# --------------------------------------------------------------------------
# Config + filler matching (pure logic)
# --------------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def normalize(text):
    """Lowercase, strip punctuation, collapse whitespace -> list of tokens."""
    text = text.lower()
    text = re.sub(r"[^a-z']+", " ", text)
    return text.split()


def build_matchers(fillers):
    """(phrase_key, token_tuple) sorted longest-first so multi-word beats single."""
    matchers = []
    for f in fillers:
        toks = tuple(normalize(f))
        if toks:
            matchers.append((f.strip().lower(), toks))
    matchers.sort(key=lambda m: len(m[1]), reverse=True)
    return matchers


def count_fillers(tokens, matchers):
    """Greedy left-to-right scan; longer phrases win, no double counting."""
    counts = {}
    i, n = 0, len(tokens)
    while i < n:
        matched = False
        for key, toks in matchers:
            L = len(toks)
            if i + L <= n and tuple(tokens[i:i + L]) == toks:
                counts[key] = counts.get(key, 0) + 1
                i += L
                matched = True
                break
        if not matched:
            i += 1
    return counts


# --------------------------------------------------------------------------
# Dual-pass recognition
#
# Pass 1 (word pass): normal Vosk decoding catches word fillers ("you know",
# "like", "i mean"...). Its language model "autocorrects" um/uh into real
# words (am/are/i'm), so it can never catch those.
#
# Pass 2 (acoustic pass): a second recognizer constrained to the grammar
# ["um", "uh", "[unk]"] over the SAME audio. With no real words available,
# filled pauses decode as um/uh and everything else falls into [unk].
# Verified: catches pause-surrounded hesitations, zero false positives on
# am/umbrella/umpteen-laden speech.
# --------------------------------------------------------------------------
AC_MIN_DUR = 0.12    # seconds — reject blips shorter than a real filled pause
AC_MAX_DUR = 2.0     # seconds — reject long [unk]-like stretches
AC_MIN_CONF = 0.7


def process_block(data, rec_word, rec_ac, matchers, ac_set, out, echo=False):
    """Feed one audio block through both recognizers; emit events to `out`."""
    # word pass
    if rec_word.AcceptWaveform(data):
        text = json.loads(rec_word.Result()).get("text", "")
        if text:
            if echo:
                print(f"heard: {text}", flush=True)
            out.put(("speech",))
            counts = count_fillers(normalize(text), matchers)
            # um/uh style fillers are counted by the acoustic pass; drop them
            # here in case a model ever does emit them (avoids double count)
            counts = {k: v for k, v in counts.items() if k not in ac_set}
            if counts:
                out.put(("final", counts))
    else:
        ptext = json.loads(rec_word.PartialResult()).get("partial", "")
        if ptext:
            # any live partial text = the user is currently speaking
            out.put(("speech",))
            hits = set(count_fillers(normalize(ptext), matchers)) - ac_set
            if hits:
                out.put(("partial", hits))
    # acoustic pass
    if rec_ac.AcceptWaveform(data):
        res = json.loads(rec_ac.Result())
        counts = {}
        for w in res.get("result", []):
            word = w.get("word")
            dur = w.get("end", 0) - w.get("start", 0)
            conf = w.get("conf", 0)
            if word in ac_set and AC_MIN_DUR <= dur <= AC_MAX_DUR and conf >= AC_MIN_CONF:
                if echo:
                    print(f"heard (acoustic): {word} ({dur:.2f}s conf {conf:.2f})", flush=True)
                counts[word] = counts.get(word, 0) + 1
        if counts:
            out.put(("final", counts))


# --------------------------------------------------------------------------
# Audio + recognition thread (pushes events to a queue)
# --------------------------------------------------------------------------
class Listener(threading.Thread):
    def __init__(self, matchers, mic_device, out_queue, acoustic_fillers=None, echo=False):
        super().__init__(daemon=True)
        self.matchers = matchers
        self.mic_device = mic_device
        self.out = out_queue
        self.acoustic = list(acoustic_fillers or ["um", "uh"])
        self.echo = echo
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            import sounddevice as sd
            from vosk import Model, KaldiRecognizer, SetLogLevel
        except Exception as e:
            self.out.put(("error", f"Missing dependency: {e}\nRun ./setup.sh first."))
            return

        SetLogLevel(-1)
        if not os.path.isdir(MODEL_DIR):
            self.out.put(("error", f"No model at {MODEL_DIR}\nRun ./setup.sh to download it."))
            return
        try:
            model = Model(MODEL_DIR)
            rec_word = KaldiRecognizer(model, SAMPLE_RATE)
            rec_word.SetWords(False)
            rec_ac = KaldiRecognizer(model, SAMPLE_RATE,
                                     json.dumps(self.acoustic + ["[unk]"]))
            rec_ac.SetWords(True)
        except Exception as e:
            self.out.put(("error", f"Failed to load model: {e}"))
            return

        audio_q = queue.Queue()

        def audio_cb(indata, frames, time_info, status):
            audio_q.put(bytes(indata))

        self.out.put(("status", "listening"))
        try:
            with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=8000,
                                   device=self.mic_device, dtype="int16",
                                   channels=1, callback=audio_cb):
                ac_set = set(self.acoustic)
                while not self._stop.is_set():
                    try:
                        data = audio_q.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    process_block(data, rec_word, rec_ac, self.matchers,
                                  ac_set, self.out, echo=self.echo)
        except Exception as e:
            self.out.put(("error", f"Audio error: {e}"))


def input_devices():
    """[(index, name)] of available input devices."""
    try:
        import sounddevice as sd
    except Exception:
        return []
    devs = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            devs.append((i, d["name"]))
    return devs


MONOLOGUE_GAP = 2.0  # seconds of silence that ends a "continuous talking" run


def monologue_limit(cfg):
    """Seconds allowed of continuous talking, or None if the warning is off."""
    mono = cfg.get("monologue", {})
    mode = mono.get("mode", "off")
    if mode == "short":
        return float(mono.get("short_seconds", 30))
    if mode == "medium":
        return float(mono.get("medium_seconds", 90))
    return None


# --------------------------------------------------------------------------
# Native Cocoa overlay
# --------------------------------------------------------------------------
def run_overlay(config, echo=False, dock=False):
    import objc
    from Cocoa import (
        NSApplication, NSApp, NSPanel, NSWindow, NSView, NSTextField, NSTextView,
        NSScrollView, NSButton, NSPopUpButton, NSColor, NSFont, NSBezierPath,
        NSObject, NSTimer, NSMakeRect,
        NSApplicationActivationPolicyAccessory, NSApplicationActivationPolicyRegular,
        NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
        NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
        NSBackingStoreBuffered, NSFloatingWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSBezelStyleRounded,
    )

    def rgb(r, g, b, a=1.0):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)

    BG = rgb(0.067, 0.075, 0.102, 0.95)
    BG_FLASH = rgb(1.0, 0.36, 0.42, 0.97)     # filler flash (red)
    BG_MONO = rgb(0.30, 0.22, 0.06, 0.97)     # monologue pulse (amber-dark)
    FG = rgb(0.91, 0.925, 0.96)
    DIM = rgb(0.486, 0.522, 0.596)
    OK = rgb(0.306, 0.788, 0.541)
    AMBER = rgb(0.957, 0.753, 0.306)
    ACCENT = rgb(1.0, 0.36, 0.42)

    W = 280
    PAD = 16
    ROW_H = 19
    GRAPH_H = 44

    class BGView(NSView):
        def initWithFrame_(self, frame):
            self = objc.super(BGView, self).initWithFrame_(frame)
            if self:
                self._bg = BG
            return self

        def setBG_(self, color):
            if color is not self._bg:
                self._bg = color
                self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                self.bounds(), 16.0, 16.0)
            self._bg.setFill()
            path.fill()

    class GraphView(NSView):
        """Timeline of filler counts per interval: bars grow left→right and
        compress so the whole call always stays visible."""

        def initWithFrame_(self, frame):
            self = objc.super(GraphView, self).initWithFrame_(frame)
            if self:
                self.buckets = [0]
                self.bucket_seconds = 30
            return self

        def drawRect_(self, rect):
            b = self.bounds()
            w, h = b.size.width, b.size.height
            # baseline
            DIM.colorWithAlphaComponent_(0.35).setFill()
            NSBezierPath.fillRect_(NSMakeRect(0, 0, w, 1))
            n = len(self.buckets)
            if n == 0:
                return
            gap = 1.0 if (w / n) >= 3 else 0.0
            bw = min(10.0, (w - gap * (n - 1)) / n)
            maxc = max(4, max(self.buckets))
            # thresholds consistent with the rate label: green <4/min, amber <8
            per_min = 60.0 / max(1, self.bucket_seconds)
            for i, c in enumerate(self.buckets):
                x = i * (bw + gap)
                if c <= 0:
                    # tick mark so elapsed empty intervals are visible
                    DIM.colorWithAlphaComponent_(0.25).setFill()
                    NSBezierPath.fillRect_(NSMakeRect(x, 1, max(1.0, bw), 2))
                    continue
                rate = c * per_min
                color = ACCENT if rate >= 8 else AMBER if rate >= 4 else OK
                color.setFill()
                bh = 3 + (c / maxc) * (h - 5)
                NSBezierPath.fillRect_(NSMakeRect(x, 1, max(1.0, bw), bh))

    def label(parent, x, y, w, h, size, color, weight_bold=False, mono=False,
              align_right=False, text=None):
        f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        f.setBezeled_(False)
        f.setDrawsBackground_(False)
        f.setEditable_(False)
        f.setSelectable_(False)
        f.setTextColor_(color)
        if mono:
            fnt = NSFont.monospacedDigitSystemFontOfSize_weight_(size, 0.0)
        elif weight_bold:
            fnt = NSFont.boldSystemFontOfSize_(size)
        else:
            fnt = NSFont.systemFontOfSize_(size)
        f.setFont_(fnt)
        if align_right:
            f.setAlignment_(2)
        if text is not None:
            f.setStringValue_(text)
        parent.addSubview_(f)
        return f

    def button(parent, x, y, w, title, target, action):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 26))
        b.setTitle_(title)
        b.setBezelStyle_(NSBezelStyleRounded)
        b.setTarget_(target)
        b.setAction_(action)
        parent.addSubview_(b)
        return b

    class Controller(NSObject):
        @objc.python_method
        def setup(self, cfg):
            self.cfg = cfg
            self.counts = {}
            self.total = 0
            self.events = []          # real timestamps, for rate/min
            self.paused = False
            self.elapsed_accum = 0.0  # active seconds before the current run
            self.active_since = time.time()
            self.flash_until = 0.0
            self.last_speech = 0.0
            self.speech_start = None
            self.q = queue.Queue()
            self.settings_win = None
            self.panel = None
            self._build_window()
            self._start_listener()
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.1, self, b"poll:", None, True)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.2, self, b"tick:", None, True)
            return self

        # ---- helpers ----
        @objc.python_method
        def active_time(self):
            if self.paused:
                return self.elapsed_accum
            return self.elapsed_accum + (time.time() - self.active_since)

        @objc.python_method
        def display_list(self):
            pri = [d.strip().lower() for d in self.cfg.get("display_priority", [])]
            if pri:
                return pri
            return (list(self.cfg.get("acoustic_fillers", [])) +
                    [f.strip().lower() for f in self.cfg.get("fillers", [])])

        @objc.python_method
        def _start_listener(self):
            matchers = build_matchers(self.cfg.get("fillers", []))
            self.listener = Listener(matchers, self.cfg.get("mic_device", None),
                                     self.q,
                                     acoustic_fillers=self.cfg.get("acoustic_fillers"),
                                     echo=echo)
            self.listener.start()

        # ---- main window ----
        @objc.python_method
        def _build_window(self):
            if self.panel is not None:
                self.panel.orderOut_(None)
            win = self.cfg.get("window", {})
            rows = self.display_list()
            H = (10 + 16 + 2 + 54 + 14 + 6 + 16 + 16 + 6 +
                 len(rows) * ROW_H + 4 + GRAPH_H + 8 + 26 + 12)

            style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setHasShadow_(True)
            panel.setLevel_(NSFloatingWindowLevel)
            panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces |
                NSWindowCollectionBehaviorFullScreenAuxiliary)
            panel.setMovableByWindowBackground_(True)
            panel.setBecomesKeyOnlyIfNeeded_(True)
            try:
                panel.setAlphaValue_(float(win.get("opacity", 0.92)))
            except Exception:
                pass

            view = BGView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
            panel.setContentView_(view)
            self.bgview = view

            y = H - 10
            # header
            y -= 16
            label(view, PAD, y, 120, 16, 10, DIM, weight_bold=True, text="FILLER COACH")
            self.dot = label(view, W - 28, y - 1, 14, 16, 13, DIM, text="●")
            gear = NSButton.alloc().initWithFrame_(NSMakeRect(W - 58, y - 4, 26, 22))
            gear.setTitle_("⚙")
            gear.setBezelStyle_(NSBezelStyleRounded)
            gear.setTarget_(self)
            gear.setAction_(b"openSettings:")
            view.addSubview_(gear)
            # big total
            y -= 2 + 54
            self.total_lbl = label(view, PAD - 2, y, W - 28, 54, 44, FG,
                                   weight_bold=True, text=str(self.total))
            y -= 14
            label(view, PAD, y, 200, 14, 10, DIM, text="fillers this session")
            # rate + timer
            y -= 6 + 16
            self.rate_lbl = label(view, PAD, y, 120, 16, 12, OK, mono=True, text="0.0 / min")
            self.timer_lbl = label(view, W - 90, y, 74, 16, 12, DIM, mono=True,
                                   align_right=True, text="00:00")
            # monologue line
            y -= 16
            self.mono_lbl = label(view, PAD, y, W - 2 * PAD, 16, 11, DIM, mono=True, text="")
            # breakdown
            y -= 6
            self.break_lbls = {}
            for phrase in rows:
                y -= ROW_H
                label(view, PAD, y, 170, 16, 11, DIM, text=f"“{phrase}”")
                cnt = label(view, W - 70, y, 54, 16, 11, FG, mono=True,
                            align_right=True, text=str(self.counts.get(phrase, 0)))
                self.break_lbls[phrase] = cnt
            # graph
            y -= 4 + GRAPH_H
            gv = GraphView.alloc().initWithFrame_(
                NSMakeRect(PAD, y, W - 2 * PAD, GRAPH_H))
            gv.bucket_seconds = int(self.cfg.get("graph", {}).get("bucket_seconds", 30))
            if getattr(self, "graph", None) is not None:
                gv.buckets = self.graph.buckets   # keep history across rebuilds
            view.addSubview_(gv)
            self.graph = gv
            # buttons
            y -= 8 + 26
            bw = (W - 2 * PAD - 16) // 3
            self.pause_btn = button(view, PAD, y, bw,
                                    "Resume" if self.paused else "Pause",
                                    self, b"togglePause:")
            button(view, PAD + bw + 8, y, bw, "Reset", self, b"reset:")
            button(view, PAD + 2 * (bw + 8), y, bw, "Quit", self, b"quit:")

            x = int(win.get("x", 40))
            y0 = int(win.get("y", 80))
            screen = panel.screen()
            sh = screen.frame().size.height if screen else 900
            panel.setFrameOrigin_((x, sh - y0 - H))
            panel.orderFrontRegardless()
            self.panel = panel

        # ---- timers ----
        def poll_(self, timer):
            now = time.time()
            try:
                while True:
                    evt = self.q.get_nowait()
                    kind = evt[0]
                    if kind == "status":
                        self.dot.setTextColor_(OK)
                    elif kind == "error":
                        self.dot.setTextColor_(ACCENT)
                        self.total_lbl.setStringValue_("!")
                        print(evt[1])
                    elif self.paused:
                        continue
                    elif kind == "final":
                        self._apply(evt[1])
                    elif kind == "partial":
                        if evt[1] and self.cfg.get("alert", {}).get("flash_on_filler", True):
                            self.flash_until = now + 0.4
                    elif kind == "speech":
                        if now - self.last_speech > MONOLOGUE_GAP:
                            self.speech_start = now
                        self.last_speech = now
            except queue.Empty:
                pass

        @objc.python_method
        def _apply(self, counts):
            if not counts:
                return
            now = time.time()
            added = 0
            for phrase, c in counts.items():
                self.counts[phrase] = self.counts.get(phrase, 0) + c
                added += c
                self.events.extend([now] * c)
            self.total += added
            self.total_lbl.setStringValue_(str(self.total))
            if self.cfg.get("alert", {}).get("flash_on_filler", True):
                self.flash_until = now + 0.5
            for phrase, lbl in self.break_lbls.items():
                lbl.setStringValue_(str(self.counts.get(phrase, 0)))
            # graph
            idx = int(self.active_time() // max(1, self.graph.bucket_seconds))
            while len(self.graph.buckets) <= idx:
                self.graph.buckets.append(0)
            self.graph.buckets[idx] += added
            self.graph.setNeedsDisplay_(True)

        def tick_(self, timer):
            now = time.time()
            active = self.active_time()
            self.timer_lbl.setStringValue_(
                ("⏸ " if self.paused else "") + f"{int(active) // 60:02d}:{int(active) % 60:02d}")
            # rate over trailing window (real time)
            rw = self.cfg.get("alert", {}).get("rate_window_seconds", 60)
            self.events = [t for t in self.events if t >= now - rw]
            span = min(rw, max(1.0, active)) if active > 0 else rw
            rate = len(self.events) * (60.0 / span)
            self.rate_lbl.setStringValue_(f"{rate:.1f} / min")
            self.rate_lbl.setTextColor_(ACCENT if rate >= 8 else AMBER if rate >= 4 else OK)
            # graph: advance empty buckets with time
            if not self.paused:
                idx = int(active // max(1, self.graph.bucket_seconds))
                if idx >= len(self.graph.buckets):
                    while len(self.graph.buckets) <= idx:
                        self.graph.buckets.append(0)
                    self.graph.setNeedsDisplay_(True)
            # monologue
            bg = BG
            limit = monologue_limit(self.cfg)
            talking = (not self.paused and self.speech_start is not None
                       and now - self.last_speech <= MONOLOGUE_GAP)
            if not talking:
                self.speech_start = None
            if limit and talking:
                talk = now - self.speech_start
                mmss = f"{int(talk) // 60}:{int(talk) % 60:02d}"
                if talk >= limit:
                    self.mono_lbl.setStringValue_(f"◼ MONOLOGUE {mmss} — wrap it up")
                    self.mono_lbl.setTextColor_(ACCENT)
                    if int(now * 2) % 2 == 0:
                        bg = BG_MONO
                elif talk >= 0.6 * limit:
                    self.mono_lbl.setStringValue_(f"talking {mmss}")
                    self.mono_lbl.setTextColor_(AMBER)
                else:
                    self.mono_lbl.setStringValue_("")
            else:
                self.mono_lbl.setStringValue_("⏸ paused" if self.paused else "")
                self.mono_lbl.setTextColor_(DIM)
            # filler flash wins over monologue pulse
            if now < self.flash_until:
                bg = BG_FLASH
            self.bgview.setBG_(bg)

        # ---- actions ----
        def togglePause_(self, sender):
            now = time.time()
            if self.paused:
                self.active_since = now
                self.paused = False
                self.pause_btn.setTitle_("Pause")
            else:
                self.elapsed_accum += now - self.active_since
                self.paused = True
                self.speech_start = None
                self.pause_btn.setTitle_("Resume")

        def reset_(self, sender):
            self.counts = {}
            self.total = 0
            self.events = []
            self.elapsed_accum = 0.0
            self.active_since = time.time()
            self.speech_start = None
            self.graph.buckets = [0]
            self.graph.setNeedsDisplay_(True)
            self.total_lbl.setStringValue_("0")
            for lbl in self.break_lbls.values():
                lbl.setStringValue_("0")

        def quit_(self, sender):
            try:
                self.listener.stop()
            except Exception:
                pass
            NSApp.terminate_(self)

        # ---- settings window ----
        def openSettings_(self, sender):
            if self.settings_win is not None:
                NSApp.activateIgnoringOtherApps_(True)
                self.settings_win.makeKeyAndOrderFront_(None)
                return
            SW, SH = 400, 470
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, SW, SH),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
                NSBackingStoreBuffered, False)
            win.setTitle_("Filler Coach Settings")
            win.setReleasedWhenClosed_(False)
            win.setLevel_(NSFloatingWindowLevel)
            v = win.contentView()

            y = SH - 34
            label(v, 20, y, 360, 16, 12, NSColor.labelColor(), weight_bold=True,
                  text="Word fillers (one per line)")
            y -= 170
            scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, y, SW - 40, 162))
            scroll.setHasVerticalScroller_(True)
            tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, SW - 40, 162))
            tv.setFont_(NSFont.userFixedPitchFontOfSize_(12))
            tv.setString_("\n".join(self.cfg.get("fillers", [])))
            scroll.setDocumentView_(tv)
            v.addSubview_(scroll)
            self.tv_fillers = tv

            y -= 30
            label(v, 20, y, 360, 16, 12, NSColor.labelColor(), weight_bold=True,
                  text="Sound fillers (acoustic pass, space-separated)")
            y -= 26
            ac = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y, SW - 40, 24))
            ac.setStringValue_(" ".join(self.cfg.get("acoustic_fillers", ["um", "uh"])))
            v.addSubview_(ac)
            self.tf_acoustic = ac

            y -= 34
            label(v, 20, y, 200, 16, 12, NSColor.labelColor(), weight_bold=True,
                  text="Monologue warning")
            mono = self.cfg.get("monologue", {})
            pop = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(210, y - 4, 170, 26), False)
            pop.addItemsWithTitles_([
                "Off",
                f"Short ({int(mono.get('short_seconds', 30))}s — interviews)",
                f"Medium ({int(mono.get('medium_seconds', 90))}s — meetings)",
            ])
            pop.selectItemAtIndex_({"off": 0, "short": 1, "medium": 2}.get(
                mono.get("mode", "off"), 0))
            v.addSubview_(pop)
            self.pop_mono = pop

            y -= 36
            label(v, 20, y, 200, 16, 12, NSColor.labelColor(), weight_bold=True,
                  text="Microphone")
            devs = input_devices()
            mpop = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(210, y - 4, 170, 26), False)
            mpop.addItemWithTitle_("System default")
            cur = self.cfg.get("mic_device", None)
            sel = 0
            for n, (i, name) in enumerate(devs):
                mpop.addItemWithTitle_(f"{i}: {name[:24]}")
                if cur == i:
                    sel = n + 1
            mpop.selectItemAtIndex_(sel)
            v.addSubview_(mpop)
            self.pop_mic = mpop
            self._mic_devs = devs

            y -= 40
            label(v, 20, y, SW - 40, 16, 11, NSColor.secondaryLabelColor(),
                  text="Tip: wear headphones so other people's voices never reach your mic.")

            button(v, SW - 190, 14, 80, "Cancel", self, b"closeSettings:")
            button(v, SW - 100, 14, 80, "Save", self, b"saveSettings:")

            win.center()
            self.settings_win = win
            NSApp.activateIgnoringOtherApps_(True)
            win.makeKeyAndOrderFront_(None)

        def closeSettings_(self, sender):
            if self.settings_win is not None:
                self.settings_win.orderOut_(None)

        def saveSettings_(self, sender):
            fillers = [ln.strip() for ln in str(self.tv_fillers.string()).split("\n")
                       if ln.strip()]
            acoustic = [w.strip().lower() for w in
                        str(self.tf_acoustic.stringValue()).split() if w.strip()]
            self.cfg["fillers"] = fillers
            self.cfg["acoustic_fillers"] = acoustic or ["um", "uh"]
            self.cfg.setdefault("monologue", {})["mode"] = \
                ["off", "short", "medium"][self.pop_mono.indexOfSelectedItem()]
            sel = self.pop_mic.indexOfSelectedItem()
            self.cfg["mic_device"] = None if sel <= 0 else self._mic_devs[sel - 1][0]
            save_config(self.cfg)
            # apply live: restart listener with new matchers, rebuild overlay rows
            try:
                self.listener.stop()
            except Exception:
                pass
            self._start_listener()
            self._build_window()
            self.settings_win.orderOut_(None)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(
        NSApplicationActivationPolicyRegular if dock
        else NSApplicationActivationPolicyAccessory)
    controller = Controller.alloc().init()
    controller.setup(config)
    _selftest = os.environ.get("FILLER_COACH_SELFTEST")
    if _selftest:
        # render for N seconds then quit — used to verify the overlay launches
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            float(_selftest), controller, b"quit:", None, False)
    app.run()


# --------------------------------------------------------------------------
def list_devices():
    try:
        import sounddevice as sd
    except Exception as e:
        print(f"sounddevice not installed ({e}). Run ./setup.sh first.")
        return
    print(sd.query_devices())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-devices", action="store_true",
                    help="list audio input devices and exit")
    ap.add_argument("--echo", action="store_true",
                    help="print recognized text to the terminal (for tuning the filler list)")
    ap.add_argument("--dock", action="store_true",
                    help="show a Dock icon (used by the FillerCoach.app bundle)")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return

    run_overlay(load_config(), echo=args.echo, dock=args.dock)


if __name__ == "__main__":
    main()
