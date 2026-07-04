#!/usr/bin/env python3
"""
Filler Killer — slay what you say. A local, real-time filler-word counter.

Listens to your microphone with an offline speech-to-text engine (Vosk),
counts filler words/phrases as you speak, and shows an always-on-top floating
overlay (native Cocoa panel — hovers even over fullscreen call windows).

No cloud, no API keys, no LLM/AI tokens — audio never leaves the Mac.

Usage:
    python coach.py                 # run the overlay
    python coach.py --echo          # also print recognized text (for tuning)
    python coach.py --dock          # show a Dock icon (used by FillerKiller.app)
    python coach.py --list-devices  # print available input devices and exit
"""

import argparse
import audioop
import json
import os
import queue
import re
import statistics
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
# Scoring
# --------------------------------------------------------------------------
SCORE_MIN_WORDS = 30      # need this many spoken words before scoring
BURST_WINDOW = 15.0       # fillers within this many seconds count as a burst


def compute_score(words, filler_times, long_turns, airtime_scored):
    """
    0–100 composite speaking score, or None if too few words yet.

    - density (weight .5): fillers per 100 spoken words. 100 pts at <=0.5,
      0 pts at >=8.
    - spread (weight .2): fraction of fillers that follow another filler
      within BURST_WINDOW seconds — clustered slips read worse than isolated
      ones. 0% bursty = 100 pts.
    - airtime (weight .3): long uninterrupted turns. -15 pts each.
      Excluded (weights renormalized) when the airtime warning is off.
    """
    if words < SCORE_MIN_WORDS:
        return None
    density = len(filler_times) / words * 100.0
    density_score = max(0.0, min(100.0, (8.0 - density) / 7.5 * 100.0))
    if len(filler_times) >= 2:
        ts = sorted(filler_times)
        bursts = sum(1 for a, b in zip(ts, ts[1:]) if b - a <= BURST_WINDOW)
        burst_score = 100.0 * (1.0 - bursts / (len(ts) - 1))
    else:
        burst_score = 100.0
    if airtime_scored:
        airtime_score = max(0.0, 100.0 - 15.0 * long_turns)
        score = 0.5 * density_score + 0.2 * burst_score + 0.3 * airtime_score
    else:
        score = (0.5 * density_score + 0.2 * burst_score) / 0.7
    return int(round(score))


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
            toks = normalize(text)
            out.put(("speech",))
            out.put(("words", len(toks)))
            counts = count_fillers(toks, matchers)
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
    def __init__(self, matchers, mic_device, out_queue, acoustic_fillers=None,
                 echo=False, echo_cancel=True):
        super().__init__(daemon=True)
        self.matchers = matchers
        self.mic_device = mic_device
        self.out = out_queue
        self.acoustic = list(acoustic_fillers or ["um", "uh"])
        self.echo = echo
        self.echo_cancel = echo_cancel
        self.backend = "portaudio"
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

        # Prefer macOS voice-processing capture (echo cancellation): the OS
        # subtracts what the Mac is playing through its speakers, so remote
        # voices on a speakerphone call are NOT counted. Verified empirically:
        # TTS through the speakers is fully recognized by plain capture but
        # suppressed to nothing under voice processing. Falls back to
        # PortAudio if unavailable, disabled, or a specific mic is pinned.
        if self.echo_cancel and self.mic_device is None:
            if self._run_engine(rec_word, rec_ac):
                return
        self._run_portaudio(sd, rec_word, rec_ac)

    def _run_engine(self, rec_word, rec_ac):
        """AVAudioEngine capture with voice processing. True = ran (or died
        after starting); False = could not start, caller should fall back."""
        import array
        try:
            from AVFoundation import AVAudioEngine
            engine = AVAudioEngine.alloc().init()
            inp = engine.inputNode()
            ok, _ = inp.setVoiceProcessingEnabled_error_(True, None)
            if not ok:
                return False
            # Voice processing ducks ALL other system audio by default, which
            # would make your callers quieter while the app runs. Dial it to
            # the minimum, voice-activity-only mode (macOS 14+; harmless no-op
            # earlier). AEC itself is unaffected by the ducking level.
            self.duck_minimized = False
            try:
                from AVFoundation import (
                    AVAudioVoiceProcessingOtherAudioDuckingConfiguration)
                inp.setVoiceProcessingOtherAudioDuckingConfiguration_(
                    AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                        True, 10))  # advanced (VAD-gated), level = Min
                self.duck_minimized = True
            except Exception:
                pass
            fmt = inp.outputFormatForBus_(0)
            sr = int(fmt.sampleRate())
            aq = queue.Queue()

            def tap(buf, when):
                try:
                    n = int(buf.frameLength())
                    aq.put(bytes(buf.floatChannelData()[0].as_buffer(4 * n)))
                except Exception:
                    pass

            inp.installTapOnBus_bufferSize_format_block_(0, 4096, fmt, tap)
            engine.prepare()
            ok, _ = engine.startAndReturnError_(None)
            if not ok:
                return False
        except Exception:
            return False

        self.backend = "voice-processing (echo cancel)"
        self.out.put(("status", "listening"))
        ac_set = set(self.acoustic)
        state = None
        started = time.time()
        diag_done = False
        max_rms = 0
        try:
            while not self._stop.is_set():
                try:
                    raw = aq.get(timeout=0.25)
                except queue.Empty:
                    continue
                floats = array.array("f", raw)
                ints = array.array("h", (
                    32767 if x >= 1.0 else -32768 if x <= -1.0 else int(x * 32767)
                    for x in floats))
                data, state = audioop.ratecv(ints.tobytes(), 2, 1, sr,
                                             SAMPLE_RATE, state)
                rms = audioop.rms(data, 2)
                max_rms = max(max_rms, rms)
                self.out.put(("level", rms))
                if not diag_done and time.time() - started > 5:
                    diag_done = True
                    self._write_diagnostic(None, max_rms)
                process_block(data, rec_word, rec_ac, self.matchers,
                              ac_set, self.out, echo=self.echo)
        except Exception as e:
            self.out.put(("error", f"Audio error: {e}"))
        finally:
            try:
                engine.stop()
            except Exception:
                pass
        return True

    def _run_portaudio(self, sd, rec_word, rec_ac):
        audio_q = queue.Queue()

        def audio_cb(indata, frames, time_info, status):
            audio_q.put(bytes(indata))

        self.out.put(("status", "listening"))
        try:
            with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=8000,
                                   device=self.mic_device, dtype="int16",
                                   channels=1, callback=audio_cb):
                ac_set = set(self.acoustic)
                started = time.time()
                diag_done = False
                max_rms = 0
                while not self._stop.is_set():
                    try:
                        data = audio_q.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    # level ping lets the UI show the mic is actually hearing
                    # something (mic permission problems produce pure silence)
                    rms = audioop.rms(data, 2)
                    max_rms = max(max_rms, rms)
                    self.out.put(("level", rms))
                    if not diag_done and time.time() - started > 5:
                        diag_done = True
                        self._write_diagnostic(sd, max_rms)
                    process_block(data, rec_word, rec_ac, self.matchers,
                                  ac_set, self.out, echo=self.echo)
        except Exception as e:
            self.out.put(("error", f"Audio error: {e}"))
            try:
                self._write_diagnostic(None, -1, error=str(e))
            except Exception:
                pass

    def _write_diagnostic(self, sd, max_rms, error=None):
        """Drop mic state into AppSupport for troubleshooting silent-mic issues."""
        info = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "backend": self.backend,
                "max_rms_first_5s": max_rms, "error": error}
        try:
            import AVFoundation
            info["tcc_status"] = int(
                AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
                    AVFoundation.AVMediaTypeAudio))  # 0=ask 1=restricted 2=denied 3=ok
        except Exception as e:
            info["tcc_status"] = f"unavailable: {e}"
        if sd is None:
            info["input_device"] = "system default"
        else:
            try:
                dev = self.mic_device
                if dev is None:
                    dev = sd.default.device[0]
                info["input_device"] = sd.query_devices(dev)["name"]
            except Exception:
                info["input_device"] = "?"
        os.makedirs(os.path.dirname(SESSIONS_DIR), exist_ok=True)
        with open(os.path.join(os.path.dirname(SESSIONS_DIR), "mic-diagnostic.json"), "w") as f:
            json.dump(info, f, indent=2)


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


# --------------------------------------------------------------------------
# Airtime (talking-turn) tracking
# --------------------------------------------------------------------------
AIRTIME_GAP = 2.0    # seconds of silence that ends a talking turn
MIN_TURN = 3.0       # ignore shorter turns ("yeah", "mm-hmm") in stats


def airtime_limit(cfg):
    """Seconds allowed of continuous talking, or None if the warning is off."""
    mono = cfg.get("monologue", {})
    mode = mono.get("mode", "off")
    if mode == "short":
        return float(mono.get("short_seconds", 30))
    if mode == "medium":
        return float(mono.get("medium_seconds", 90))
    return None


def fmt_mmss(seconds):
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


# --------------------------------------------------------------------------
# App identity + sessions
# --------------------------------------------------------------------------
__version__ = "1.3.2"
GITHUB_URL = "https://github.com/mattbakerpm/filler-killer"

ABOUT_TEXT = (
    "Hi, I'm Matt. I say “um” and “you know” way too much — "
    "at a genuinely distracting level — and I wanted live feedback during real "
    "calls, not a report afterwards, and definitely not my meeting audio "
    "shipped to someone's cloud.\n\n"
    "So Filler Killer runs entirely on your Mac: an offline speech engine, a "
    "floating counter, and zero network calls. If it helps you slay what you "
    "say too, that's the whole point. It's free and open source — issues and "
    "PRs welcome."
)

SESSIONS_DIR = os.path.expanduser(
    "~/Library/Application Support/FillerKiller/sessions")


def list_sessions():
    """All saved sessions, newest first."""
    out = []
    try:
        for fn in os.listdir(SESSIONS_DIR):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(SESSIONS_DIR, fn)) as f:
                        s = json.load(f)
                    s["_file"] = fn
                    out.append(s)
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    out.sort(key=lambda s: s.get("id", ""), reverse=True)
    return out


def write_session(sess):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = os.path.join(SESSIONS_DIR, sess["id"] + ".json")
    with open(path, "w") as f:
        json.dump({k: v for k, v in sess.items() if not k.startswith("_")},
                  f, indent=2)


def delete_session(sess):
    try:
        os.remove(os.path.join(SESSIONS_DIR, sess["_file"]))
    except OSError:
        pass


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
        NSMenu, NSMenuItem, NSImage, NSImageView, NSTableView, NSTableColumn,
        NSWorkspace, NSURL, NSAttributedString, NSPNGFileType,
        NSFontAttributeName, NSForegroundColorAttributeName, NSImageLeft,
    )
    from Foundation import NSBundle

    # The GUI process is the framework python, so the menu bar would say
    # "Python". Patching CFBundleName in the live info dictionary BEFORE
    # NSApplication is initialized makes the app menu say "Filler Killer".
    try:
        info = (NSBundle.mainBundle().localizedInfoDictionary()
                or NSBundle.mainBundle().infoDictionary())
        if info is not None:
            info["CFBundleName"] = "Filler Killer"
    except Exception:
        pass

    def rgb(r, g, b, a=1.0):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)

    BG = rgb(0.067, 0.075, 0.102, 0.98)
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
        def drawRect_(self, rect):
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                self.bounds(), 16.0, 16.0)
            BG.setFill()
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
            DIM.colorWithAlphaComponent_(0.35).setFill()
            NSBezierPath.fillRect_(NSMakeRect(0, 0, w, 1))
            n = len(self.buckets)
            if n == 0:
                return
            gap = 1.0 if (w / n) >= 3 else 0.0
            bw = min(10.0, (w - gap * (n - 1)) / n)
            maxc = max(4, max(self.buckets))
            per_min = 60.0 / max(1, self.bucket_seconds)
            for i, c in enumerate(self.buckets):
                x = i * (bw + gap)
                if c <= 0:
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

    def button(parent, x, y, w, title, target, action, h=26):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
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
            self.words_total = 0
            self.events = []            # recent timestamps, for rate/min
            self.filler_times = []      # all timestamps, for burstiness
            self.turns = []             # completed talking-turn durations
            self.long_turns = 0
            self.run_flagged = False
            self.paused = False
            self.expanded = False
            self.elapsed_accum = 0.0
            self.active_since = time.time()
            self.flash_until = 0.0
            self.flash_color = AMBER
            self.last_speech = 0.0
            self.speech_start = None
            self.q = queue.Queue()
            self.settings_win = None
            self.about_win = None
            self.history_win = None
            self.hist_sessions = []
            self.mic_ok = False
            self.listen_started = None
            self.last_loud = 0.0
            self.max_rms = 0
            self.boot_time = time.time()
            self.tcc_checked = False
            self.mic_denied = False
            self.saved_until = 0.0
            self._new_session()
            self.last_autosave = time.time()
            self.panel = None
            self.graph = None
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
        def all_phrases(self):
            return (list(self.cfg.get("acoustic_fillers", [])) +
                    [f.strip().lower() for f in self.cfg.get("fillers", [])])

        @objc.python_method
        def sorted_counts(self):
            return sorted(self.all_phrases(),
                          key=lambda p: (-self.counts.get(p, 0), p))

        @objc.python_method
        def _start_listener(self):
            matchers = build_matchers(self.cfg.get("fillers", []))
            self.listener = Listener(matchers, self.cfg.get("mic_device", None),
                                     self.q,
                                     acoustic_fillers=self.cfg.get("acoustic_fillers"),
                                     echo=echo,
                                     echo_cancel=self.cfg.get("echo_cancel", True))
            self.listener.start()

        # ---- main window ----
        @objc.python_method
        def _build_window(self):
            # preserve the top-left corner across rebuilds (accordion toggles)
            if self.panel is not None:
                f = self.panel.frame()
                origin_x, top = f.origin.x, f.origin.y + f.size.height
                self.panel.orderOut_(None)
            else:
                win = self.cfg.get("window", {})
                origin_x = int(win.get("x", 40))
                top = None  # resolved after we know screen height

            rows = self.sorted_counts() if self.expanded else []
            H = (10 + 16 + 2 + 54 + 14 + 6 + 16 + 16 + 6 +
                 GRAPH_H + 6 + 18 + len(rows) * ROW_H + 8 + 26 + 12)

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
                panel.setAlphaValue_(float(self.cfg.get("window", {}).get("opacity", 0.97)))
            except Exception:
                pass

            view = BGView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
            panel.setContentView_(view)

            y = H - 10
            # header
            y -= 16
            label(view, PAD, y, 120, 16, 10, DIM, weight_bold=True, text="FILLER KILLER")
            self.dot = label(view, W - 28, y - 1, 14, 16, 13, DIM, text="●")
            def sym_btn(x, symbol, fallback, action, tip):
                b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y - 4, 28, 22))
                img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    symbol, None)
                if img is not None:
                    b.setImage_(img)
                    b.setTitle_("")
                else:
                    b.setTitle_(fallback)
                b.setBezelStyle_(NSBezelStyleRounded)
                b.setTarget_(self)
                b.setAction_(action)
                b.setToolTip_(tip)
                view.addSubview_(b)
                return b

            sym_btn(W - 60, "gearshape", "⚙", b"openSettings:", "Settings (⌘,)")
            sym_btn(W - 92, "clock.arrow.circlepath", "H", b"openHistory:",
                    "Session History (⌘Y)")
            # big row: total (left) + score (right)
            y -= 2 + 54
            self.total_lbl = label(view, PAD - 2, y, 120, 54, 44, FG,
                                   weight_bold=True, text=str(self.total))
            self.score_lbl = label(view, W - 130, y, 114, 54, 44, DIM,
                                   weight_bold=True, align_right=True, text="—")
            y -= 14
            label(view, PAD, y, 120, 14, 10, DIM, text="fillers")
            label(view, W - 130, y, 114, 14, 10, DIM, align_right=True, text="score")
            # rate + timer
            y -= 6 + 16
            self.rate_lbl = label(view, PAD, y, 120, 16, 12, OK, mono=True, text="0.0 / min")
            self.timer_lbl = label(view, W - 90, y, 74, 16, 12, DIM, mono=True,
                                   align_right=True, text="00:00")
            # airtime stats / warning line
            y -= 16
            self.air_lbl = label(view, PAD, y, W - 2 * PAD, 16, 11, DIM, mono=True, text="")
            # graph (above the words)
            y -= 6 + GRAPH_H
            gv = GraphView.alloc().initWithFrame_(
                NSMakeRect(PAD, y, W - 2 * PAD, GRAPH_H))
            gv.bucket_seconds = int(self.cfg.get("graph", {}).get("bucket_seconds", 30))
            if self.graph is not None:
                gv.buckets = self.graph.buckets
            view.addSubview_(gv)
            self.graph = gv
            # words accordion
            y -= 6 + 18
            acc = NSButton.alloc().initWithFrame_(NSMakeRect(PAD - 4, y, W - 2 * PAD + 4, 18))
            acc.setBordered_(False)
            acc.setTarget_(self)
            acc.setAction_(b"toggleWords:")
            acc.setAlignment_(0)
            acc.setImagePosition_(NSImageLeft)
            try:
                acc.setContentTintColor_(DIM)   # tints the template chevron
            except Exception:
                pass
            view.addSubview_(acc)
            self.acc_btn = acc
            self.row_lbls = []
            for phrase in rows:
                y -= ROW_H
                name = label(view, PAD, y, 170, 16, 11, DIM, text="")
                cnt = label(view, W - 70, y, 54, 16, 11, FG, mono=True,
                            align_right=True, text="")
                self.row_lbls.append((name, cnt))
            self._refresh_words()
            # buttons
            y -= 8 + 26
            bw = (W - 2 * PAD - 16) // 3
            self.pause_btn = button(view, PAD, y, bw,
                                    "Resume" if self.paused else "Pause",
                                    self, b"togglePause:")
            end_btn = button(view, PAD + bw + 8, y, bw, "End", self, b"reset:")
            end_btn.setToolTip_(
                "End session — saves to History, starts a new one "
                "(also happens automatically after silence)")
            button(view, PAD + 2 * (bw + 8), y, bw, "Quit", self, b"quit:")

            if top is None:
                screen = panel.screen()
                sh = screen.frame().size.height if screen else 900
                top = sh - int(self.cfg.get("window", {}).get("y", 80))
            panel.setFrameOrigin_((origin_x, top - H))
            panel.orderFrontRegardless()
            self.panel = panel

        @objc.python_method
        def _refresh_words(self):
            ordered = self.sorted_counts()
            title = "words"
            top = [p for p in ordered if self.counts.get(p, 0) > 0][:1]
            if not self.expanded and top:
                title += f"  ·  top “{top[0]}” ×{self.counts[top[0]]}"
            chevron = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "chevron.down" if self.expanded else "chevron.right", None)
            if chevron is not None:
                self.acc_btn.setImage_(chevron)
            else:
                title = ("▾ " if self.expanded else "▸ ") + title
            self.acc_btn.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    " " + title, {NSFontAttributeName: NSFont.systemFontOfSize_(11),
                                  NSForegroundColorAttributeName: DIM}))
            for (name, cnt), phrase in zip(self.row_lbls, ordered):
                name.setStringValue_(f"“{phrase}”")
                cnt.setStringValue_(str(self.counts.get(phrase, 0)))

        # ---- timers ----
        def poll_(self, timer):
            now = time.time()
            try:
                while True:
                    evt = self.q.get_nowait()
                    kind = evt[0]
                    if kind == "status":
                        self.mic_ok = True
                        self.listen_started = now
                        self.dot.setTextColor_(OK)
                    elif kind == "error":
                        self.mic_ok = False
                        self.dot.setTextColor_(ACCENT)
                        self.total_lbl.setStringValue_("!")
                        print(evt[1])
                    elif kind == "level":
                        rms = evt[1]
                        self.max_rms = max(self.max_rms, rms)
                        if rms > 400:
                            self.last_loud = now
                    elif self.paused:
                        continue
                    elif kind == "final":
                        self._apply(evt[1])
                    elif kind == "words":
                        self.words_total += evt[1]
                    elif kind == "partial":
                        if evt[1] and self.cfg.get("alert", {}).get("flash_on_filler", True):
                            self._set_flash(now, 0.3)
                    elif kind == "speech":
                        if now - self.last_speech > AIRTIME_GAP:
                            self.speech_start = now
                            self.run_flagged = False
                        self.last_speech = now
            except queue.Empty:
                pass

        @objc.python_method
        def _set_flash(self, now, dur):
            rw = self.cfg.get("alert", {}).get("rate_window_seconds", 60)
            recent = [t for t in self.events if t >= now - rw]
            rate = len(recent) * (60.0 / max(1.0, min(rw, max(1.0, self.active_time()))))
            self.flash_color = ACCENT if rate >= 8 else AMBER
            self.flash_until = now + dur

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
                self.filler_times.extend([now] * c)
            self.total += added
            self.total_lbl.setStringValue_(str(self.total))
            if self.cfg.get("alert", {}).get("flash_on_filler", True):
                self._set_flash(now, 0.5)
            self._refresh_words()
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
            # rate over trailing window
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
            # airtime turn tracking
            limit = airtime_limit(self.cfg)
            stat_limit = limit or float(self.cfg.get("monologue", {}).get("medium_seconds", 90))
            talking = (not self.paused and self.speech_start is not None
                       and now - self.last_speech <= AIRTIME_GAP)
            if not talking and self.speech_start is not None:
                self._end_turn()
            if talking:
                talk = now - self.speech_start
                if talk >= stat_limit and not self.run_flagged:
                    self.long_turns += 1
                    self.run_flagged = True
                if limit and talk >= limit:
                    self.air_lbl.setStringValue_(f"◼ WRAP IT UP · {fmt_mmss(talk)}")
                    self.air_lbl.setTextColor_(ACCENT)
                elif limit and talk >= 0.6 * limit:
                    self.air_lbl.setStringValue_(f"talking {fmt_mmss(talk)}")
                    self.air_lbl.setTextColor_(AMBER)
                else:
                    self._show_air_stats()
            else:
                self._show_air_stats()
            # score
            score = compute_score(self.words_total, self.filler_times,
                                  self.long_turns, limit is not None)
            if score is None:
                self.score_lbl.setStringValue_("—")
                self.score_lbl.setTextColor_(DIM)
            else:
                self.score_lbl.setStringValue_(str(score))
                self.score_lbl.setTextColor_(
                    OK if score >= 85 else AMBER if score >= 65 else ACCENT)
            # number flash (background never changes)
            self.total_lbl.setTextColor_(
                self.flash_color if now < self.flash_until else FG)
            # mic health: bright dot while sound is coming in, dim when quiet,
            # warning if the stream is open but only silence arrives (that's
            # what a missing/denied Microphone permission looks like)
            if self.mic_ok:
                if now - self.last_loud < 0.8:
                    self.dot.setTextColor_(OK)
                else:
                    self.dot.setTextColor_(OK.colorWithAlphaComponent_(0.35))
                if (self.max_rms < 10 and self.listen_started
                        and now - self.listen_started > 6):
                    self.air_lbl.setStringValue_(
                        "⚠ mic silent — check Microphone permission")
                    self.air_lbl.setTextColor_(AMBER)
            # one-time mic authorization check (3s after boot, past the async
            # permission request). If denied, tell the user and take them
            # straight to the right Settings pane.
            if not self.tcc_checked and now - self.boot_time > 3:
                self.tcc_checked = True
                try:
                    import AVFoundation
                    st = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
                        AVFoundation.AVMediaTypeAudio)
                    if st == 2:  # denied
                        self.mic_denied = True
                        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(
                            "x-apple.systempreferences:com.apple.preference.security"
                            "?Privacy_Microphone"))
                except Exception:
                    pass
            if self.mic_denied:
                self.air_lbl.setStringValue_(
                    "⚠ mic DENIED — enable Filler Killer in Settings, relaunch")
                self.air_lbl.setTextColor_(ACCENT)
                self.dot.setTextColor_(ACCENT)
            # auto-end: a meaningful session followed by long silence means
            # the call is over — save it and start fresh for the next one
            auto_min = self.cfg.get("session", {}).get("auto_end_minutes", 3)
            if (auto_min and not self.paused and self.last_speech > 0
                    and now - self.last_speech > auto_min * 60
                    and self._session_meaningful()):
                self.reset_(None)
            if now < self.saved_until:
                self.air_lbl.setStringValue_("✓ session saved to History")
                self.air_lbl.setTextColor_(OK)
            # autosave the session twice a minute
            if now - self.last_autosave > 30:
                self.last_autosave = now
                self._save_session()

        # ---- session persistence ----
        @objc.python_method
        def _new_session(self):
            t = time.localtime()
            self.session_id = time.strftime("%Y%m%d-%H%M%S", t)
            self.session_name = time.strftime("%b %-d · %-I:%M %p", t)
            self.session_started = time.strftime("%Y-%m-%dT%H:%M:%S", t)

        @objc.python_method
        def _session_meaningful(self):
            return (self.words_total >= 30 or
                    (self.total >= 1 and self.active_time() >= 60))

        @objc.python_method
        def _save_session(self):
            """Autosave the running session (idempotent per session_id)."""
            if not self._session_meaningful():
                return
            active = self.active_time()
            limit = airtime_limit(self.cfg)
            med = statistics.median(self.turns) if self.turns else None
            write_session({
                "id": self.session_id,
                "name": self.session_name,
                "started": self.session_started,
                "duration": round(active),
                "words": self.words_total,
                "fillers": self.total,
                "counts": self.counts,
                "score": compute_score(self.words_total, self.filler_times,
                                       self.long_turns, limit is not None),
                "rate": round(self.total / max(1.0, active / 60.0), 2),
                "turns": len(self.turns),
                "median_turn": round(med, 1) if med is not None else None,
                "long_turns": self.long_turns,
                "buckets": self.graph.buckets,
                "bucket_seconds": self.graph.bucket_seconds,
            })

        @objc.python_method
        def _end_turn(self):
            dur = self.last_speech - self.speech_start
            if dur >= MIN_TURN:
                self.turns.append(dur)
            self.speech_start = None
            self.run_flagged = False

        @objc.python_method
        def _show_air_stats(self):
            if self.paused:
                self.air_lbl.setStringValue_("⏸ paused")
                self.air_lbl.setTextColor_(DIM)
                return
            med = fmt_mmss(statistics.median(self.turns)) if self.turns else "—"
            self.air_lbl.setStringValue_(
                f"turns {len(self.turns)} · median {med} · long {self.long_turns}")
            self.air_lbl.setTextColor_(AMBER if self.long_turns else DIM)

        # ---- actions ----
        def toggleWords_(self, sender):
            self.expanded = not self.expanded
            self._build_window()

        def togglePause_(self, sender):
            now = time.time()
            if self.paused:
                self.active_since = now
                self.paused = False
                self.pause_btn.setTitle_("Pause")
            else:
                self.elapsed_accum += now - self.active_since
                self.paused = True
                if self.speech_start is not None:
                    self._end_turn()
                self.pause_btn.setTitle_("Resume")

        def reset_(self, sender):
            if self._session_meaningful():
                self.saved_until = time.time() + 4   # brief "saved ✓" note
            self._save_session()      # finalize the old session first
            self._new_session()
            self.counts = {}
            self.total = 0
            self.words_total = 0
            self.events = []
            self.filler_times = []
            self.turns = []
            self.long_turns = 0
            self.run_flagged = False
            self.elapsed_accum = 0.0
            self.active_since = time.time()
            self.speech_start = None
            self.graph.buckets = [0]
            self.graph.setNeedsDisplay_(True)
            self.total_lbl.setStringValue_("0")
            self.score_lbl.setStringValue_("—")
            self.score_lbl.setTextColor_(DIM)
            self._refresh_words()

        def quit_(self, sender):
            try:
                self._save_session()
            except Exception:
                pass
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
            win.setTitle_("Filler Killer Settings")
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
                  text="Airtime warning")
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

        # ---- About window ----
        def openAbout_(self, sender):
            if self.about_win is not None:
                NSApp.activateIgnoringOtherApps_(True)
                self.about_win.makeKeyAndOrderFront_(None)
                return
            AW, AH = 440, 560
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, AW, AH),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
                NSBackingStoreBuffered, False)
            win.setTitle_("About Filler Killer")
            win.setReleasedWhenClosed_(False)
            win.setLevel_(NSFloatingWindowLevel)
            v = win.contentView()

            # full brand lockup (mark + wordmark + tagline)
            logo_path = os.path.join(HERE, "assets", "filler-killer-logo.svg")
            img = NSImage.alloc().initWithContentsOfFile_(logo_path)
            if img is not None:
                iv = NSImageView.alloc().initWithFrame_(
                    NSMakeRect((AW - 270) / 2, AH - 290, 270, 270))
                iv.setImage_(img)
                v.addSubview_(iv)

            def alabel(y, h, size, text, bold=False, dim=False, wrap=False):
                f = NSTextField.alloc().initWithFrame_(NSMakeRect(28, y, AW - 56, h))
                f.setBezeled_(False)
                f.setDrawsBackground_(False)
                f.setEditable_(False)
                f.setSelectable_(False)
                f.setAlignment_(1 if not wrap else 0)  # centered / natural
                f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                           else NSFont.systemFontOfSize_(size))
                f.setTextColor_(NSColor.secondaryLabelColor() if dim
                                else NSColor.labelColor())
                f.setStringValue_(text)
                v.addSubview_(f)
                return f

            alabel(AH - 318, 16, 11,
                   f"v{__version__} · 100% local · no cloud, no tokens · MIT",
                   dim=True)
            alabel(64, 230, 12, ABOUT_TEXT, wrap=True)

            gh = button(v, AW - 130, 16, 102, "GitHub", self, b"openGitHub:")
            gh.setToolTip_(GITHUB_URL)
            button(v, 28, 16, 90, "Close", self, b"closeAbout:")

            win.center()
            self.about_win = win
            NSApp.activateIgnoringOtherApps_(True)
            win.makeKeyAndOrderFront_(None)

        def closeAbout_(self, sender):
            if self.about_win is not None:
                self.about_win.orderOut_(None)

        def openGitHub_(self, sender):
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(GITHUB_URL))

        # ---- History window ----
        def openHistory_(self, sender):
            self.hist_sessions = list_sessions()
            if self.history_win is not None:
                self.hist_table.reloadData()
                self.hist_spark.setNeedsDisplay_(True)
                NSApp.activateIgnoringOtherApps_(True)
                self.history_win.makeKeyAndOrderFront_(None)
                return
            HW, HH = 600, 440
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, HW, HH),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
                NSBackingStoreBuffered, False)
            win.setTitle_("Session History")
            win.setReleasedWhenClosed_(False)
            win.setLevel_(NSFloatingWindowLevel)
            v = win.contentView()

            controller = self

            class SparkView(NSView):
                def drawRect_(self, rect):
                    b = self.bounds()
                    w, h = b.size.width, b.size.height
                    sess = [s for s in reversed(controller.hist_sessions)
                            if s.get("score") is not None][-40:]
                    NSColor.secondaryLabelColor().colorWithAlphaComponent_(0.3).setFill()
                    NSBezierPath.fillRect_(NSMakeRect(0, 0, w, 1))
                    if not sess:
                        return
                    n = len(sess)
                    gap = 2.0
                    bw = min(16.0, (w - gap * (n - 1)) / n)
                    for i, s in enumerate(sess):
                        sc = s["score"]
                        color = OK if sc >= 85 else AMBER if sc >= 65 else ACCENT
                        color.setFill()
                        NSBezierPath.fillRect_(NSMakeRect(
                            i * (bw + gap), 1, bw, 3 + (sc / 100.0) * (h - 4)))

            lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(20, HH - 30, 300, 16))
            lbl.setBezeled_(False); lbl.setDrawsBackground_(False)
            lbl.setEditable_(False); lbl.setSelectable_(False)
            lbl.setFont_(NSFont.boldSystemFontOfSize_(12))
            lbl.setStringValue_("Score over time  (oldest → newest)")
            v.addSubview_(lbl)
            spark = SparkView.alloc().initWithFrame_(NSMakeRect(20, HH - 92, HW - 40, 56))
            v.addSubview_(spark)
            self.hist_spark = spark

            table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, HW - 40, 280))
            cols = [("name", "Session (double-click to rename)", 190, True),
                    ("duration", "Length", 60, False),
                    ("words", "Words", 60, False),
                    ("fillers", "Fillers", 55, False),
                    ("rate", "/min", 50, False),
                    ("score", "Score", 50, False)]
            for ident, title, width, editable in cols:
                c = NSTableColumn.alloc().initWithIdentifier_(ident)
                c.headerCell().setStringValue_(title)
                c.setWidth_(width)
                c.setEditable_(editable)
                table.addTableColumn_(c)
            table.setDataSource_(self)
            table.setDelegate_(self)
            table.setUsesAlternatingRowBackgroundColors_(True)
            scroll = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(20, 56, HW - 40, HH - 160))
            scroll.setHasVerticalScroller_(True)
            scroll.setDocumentView_(table)
            v.addSubview_(scroll)
            self.hist_table = table

            button(v, 20, 14, 110, "Delete", self, b"deleteSession:")
            button(v, HW - 110, 14, 90, "Close", self, b"closeHistory:")

            win.center()
            self.history_win = win
            NSApp.activateIgnoringOtherApps_(True)
            win.makeKeyAndOrderFront_(None)

        def closeHistory_(self, sender):
            if self.history_win is not None:
                self.history_win.orderOut_(None)

        def deleteSession_(self, sender):
            row = self.hist_table.selectedRow()
            if 0 <= row < len(self.hist_sessions):
                delete_session(self.hist_sessions[row])
                self.hist_sessions = list_sessions()
                self.hist_table.reloadData()
                self.hist_spark.setNeedsDisplay_(True)

        # NSTableView data source (history)
        def numberOfRowsInTableView_(self, table):
            return len(self.hist_sessions)

        def tableView_objectValueForTableColumn_row_(self, table, col, row):
            s = self.hist_sessions[row]
            ident = str(col.identifier())
            if ident == "name":
                return s.get("name", s.get("id", "?"))
            if ident == "duration":
                return fmt_mmss(s.get("duration", 0))
            if ident == "score":
                sc = s.get("score")
                return "—" if sc is None else str(sc)
            if ident == "rate":
                return f"{s.get('rate', 0):.1f}"
            return str(s.get(ident, ""))

        def tableView_setObjectValue_forTableColumn_row_(self, table, value, col, row):
            if str(col.identifier()) == "name" and 0 <= row < len(self.hist_sessions):
                s = self.hist_sessions[row]
                s["name"] = str(value).strip() or s["name"]
                write_session(s)
                if s.get("id") == self.session_id:
                    self.session_name = s["name"]

        # ---- self-test hooks ----
        def snapshot_(self, timer):
            """FILLER_COACH_SNAPSHOT=<path-prefix>: render the panel to PNGs
            (collapsed then expanded) so appearance can be verified headlessly."""
            step = getattr(self, "_snap_step", 0)
            self._snap_step = step + 1
            prefix = os.environ.get("FILLER_COACH_SNAPSHOT", "/tmp/fk-panel")
            if step == 0:
                self._apply({"um": 2, "like": 1})   # give the UI real content
            elif step in (1, 3):
                v = self.panel.contentView()
                rep = v.bitmapImageRepForCachingDisplayInRect_(v.bounds())
                v.cacheDisplayInRect_toBitmapImageRep_(v.bounds(), rep)
                png = rep.representationUsingType_properties_(NSPNGFileType, None)
                png.writeToFile_atomically_(f"{prefix}-{'collapsed' if step == 1 else 'expanded'}.png", True)
                if step == 1:
                    self.toggleWords_(None)
                else:
                    print("SNAPSHOTS DONE", flush=True)

        # ---- self-test hook (FILLER_COACH_EXERCISE=1): drive the UI paths ----
        def exercise_(self, timer):
            step = getattr(self, "_ex_step", 0)
            self._ex_step = step + 1
            if step == 0:
                self.toggleWords_(None)           # expand
            elif step == 1:
                self._apply({"um": 2, "like": 1})  # counts + flash + graph
                self.words_total += 40
            elif step == 2:
                self.toggleWords_(None)           # collapse (keeps counts)
            elif step == 3:
                self.togglePause_(None)           # pause
            elif step == 4:
                self.togglePause_(None)           # resume
                assert self.total == 3, f"total lost across rebuilds: {self.total}"
                assert "×2" in str(self.acc_btn.title()), "top word not in accordion title"
            elif step == 5:
                self.words_total += 100           # make session meaningful
                self._save_session()
                assert any(s["id"] == self.session_id for s in list_sessions()), \
                    "session not persisted"
                self.openAbout_(None)
                self.closeAbout_(None)
                self.openHistory_(None)
                assert self.hist_table.numberOfRows() >= 1, "history table empty"
                self.closeHistory_(None)
                delete_session({"_file": self.session_id + ".json"})  # clean up test session
            elif step == 6:
                # trigger auto-end: meaningful session + fake long silence
                self.words_total = 100
                self._ex_old_sid = self.session_id
                self.last_speech = time.time() - 9999
            elif step == 7:
                assert self.session_id != self._ex_old_sid, "auto-end did not fire"
                assert self.total == 0, "auto-end did not clear counters"
                delete_session({"_file": self._ex_old_sid + ".json"})
                print("EXERCISE OK", flush=True)

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
            # apply live: restart listener with new matchers, rebuild overlay
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
    # Explicitly request mic access up front so the TCC prompt appears at
    # launch (attributed to this app) instead of being silently auto-denied
    # when the audio stream opens. No-op if permission is already decided.
    try:
        import AVFoundation
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, lambda granted: None)
    except Exception:
        pass  # framework not installed (e.g. ./run.sh venv) — Terminal's grant applies
    controller = Controller.alloc().init()
    controller.setup(config)

    # real app menu (About / Settings / History / Quit)
    main_menu = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    main_menu.addItem_(app_item)
    app_menu = NSMenu.alloc().initWithTitle_("Filler Killer")

    def mitem(title, action, key, modifier_free=False):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        it.setTarget_(controller)
        app_menu.addItem_(it)
        return it

    mitem("About Filler Killer", b"openAbout:", "")
    app_menu.addItem_(NSMenuItem.separatorItem())
    mitem("Settings…", b"openSettings:", ",")
    mitem("Session History…", b"openHistory:", "y")
    app_menu.addItem_(NSMenuItem.separatorItem())
    mitem("Quit Filler Killer", b"quit:", "q")
    app_item.setSubmenu_(app_menu)
    NSApp.setMainMenu_(main_menu)
    _selftest = os.environ.get("FILLER_COACH_SELFTEST")
    if _selftest:
        # render for N seconds then quit — used to verify the overlay launches
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            float(_selftest), controller, b"quit:", None, False)
        if os.environ.get("FILLER_COACH_EXERCISE"):
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.4, controller, b"exercise:", None, True)
        if os.environ.get("FILLER_COACH_SNAPSHOT"):
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.5, controller, b"snapshot:", None, True)
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
                    help="show a Dock icon (used by the FillerKiller.app bundle)")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return

    run_overlay(load_config(), echo=args.echo, dock=args.dock)


if __name__ == "__main__":
    main()
