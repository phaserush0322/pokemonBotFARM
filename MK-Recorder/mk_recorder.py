"""
MK-Recorder: Mouse & Keyboard Macro Recorder/Player with Screen Detection
Records mouse movements, clicks, and keyboard inputs then replays them.
Detects when screen stops changing (character idle) and auto-replays macro.

Hotkeys:
    F6  - Start/Stop Recording
    F7  - Start/Stop Playback
    F8  - Pause/Resume Playback
    V   - Start/Stop Auto-Farm
    F9  - Exit App
"""

import tkinter as tk
from tkinter import filedialog
import threading
import time
import json
import os
import ctypes
from datetime import datetime

import numpy as np
from PIL import Image, ImageTk
import mss

# Fix DPI scaling on Windows - MUST be called before any GUI
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from pynput import mouse, keyboard
from pynput.mouse import Button as MouseButton, Controller as MouseController
from pynput.keyboard import Key, Controller as KeyboardController


# ══════════════════════════════════════════════════════════════
#  Logger
# ══════════════════════════════════════════════════════════════

class Logger:
    def __init__(self, text_widget, log_file="mk_recorder.log"):
        self.text_widget = text_widget
        self.log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_file)
        # Clear old log on start
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(f"=== MK-Recorder Log Started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    def log(self, msg, tag="info"):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{timestamp}] {msg}"

        # Write to file
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        # Write to text widget (thread-safe via after)
        def _append():
            self.text_widget.config(state="normal")
            self.text_widget.insert("end", line + "\n", tag)
            self.text_widget.see("end")
            # Keep only last 200 lines
            lines = int(self.text_widget.index("end-1c").split(".")[0])
            if lines > 200:
                self.text_widget.delete("1.0", f"{lines - 200}.0")
            self.text_widget.config(state="disabled")

        try:
            self.text_widget.after(0, _append)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
#  Screen Watcher
# ══════════════════════════════════════════════════════════════

class ScreenWatcher:
    def __init__(self):
        self.region = None
        self._prev_frame = None
        self._local = threading.local()

    def _get_sct(self):
        """Get a thread-local mss instance (mss is not thread-safe)."""
        if not hasattr(self._local, 'sct'):
            self._local.sct = mss.mss()
        return self._local.sct

    def capture_region(self):
        if not self.region:
            return None
        screenshot = self._get_sct().grab(self.region)
        return np.array(Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX"))

    def get_change_percent(self):
        if not self.region:
            return 0
        current = self.capture_region()
        if current is None:
            return 0
        if self._prev_frame is None or current.shape != self._prev_frame.shape:
            self._prev_frame = current
            return 100
        diff = np.abs(current.astype(np.int16) - self._prev_frame.astype(np.int16))
        change = (np.sum(diff) / (255 * current.size)) * 100
        self._prev_frame = current
        return round(change, 2)

    def reset(self):
        self._prev_frame = None


# ══════════════════════════════════════════════════════════════
#  Battle Detector - detects a button/image on screen and clicks it
# ══════════════════════════════════════════════════════════════

class BattleDetector:
    def __init__(self):
        self.enabled = False
        self.run_button_region = None   # {"left", "top", "width", "height"}
        self.run_button_image = None    # numpy array of what "Run" button looks like
        self.match_threshold = 85       # similarity % to consider a match
        self._local = threading.local()

    def _get_sct(self):
        if not hasattr(self._local, 'sct'):
            self._local.sct = mss.mss()
        return self._local.sct

    def capture_run_button(self):
        """Capture what the Run button looks like right now."""
        if not self.run_button_region:
            return None
        screenshot = self._get_sct().grab(self.run_button_region)
        self.run_button_image = np.array(
            Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        )
        return self.run_button_image

    def get_similarity(self):
        """Get similarity % between current screen and captured Run button. Returns (similarity, is_match)."""
        if self.run_button_image is None or not self.run_button_region:
            return 0, False
        try:
            screenshot = self._get_sct().grab(self.run_button_region)
            current = np.array(
                Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
            )
            if current.shape != self.run_button_image.shape:
                return 0, False
            diff = np.abs(current.astype(np.int16) - self.run_button_image.astype(np.int16))
            similarity = round((1 - np.sum(diff) / (255 * current.size)) * 100, 1)
            return similarity, similarity >= self.match_threshold
        except Exception as e:
            return -1, False

    def is_battle(self):
        """Check if the Run button is currently visible on screen."""
        if not self.enabled:
            return False
        _, matched = self.get_similarity()
        return matched

    def click_run_button(self):
        """Click the center of the Run button region."""
        if not self.run_button_region:
            return
        mc = MouseController()
        cx = self.run_button_region["left"] + self.run_button_region["width"] // 2
        cy = self.run_button_region["top"] + self.run_button_region["height"] // 2
        mc.position = (cx, cy)
        time.sleep(0.1)
        mc.click(MouseButton.left)


# ══════════════════════════════════════════════════════════════
#  Region Selector
# ══════════════════════════════════════════════════════════════

class RegionSelector:
    def __init__(self, on_selected):
        self.on_selected = on_selected
        self.start_x = 0
        self.start_y = 0

        self.root = tk.Toplevel()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.3)
        self.root.configure(bg="black", cursor="crosshair")

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        self.canvas.create_text(
            self.root.winfo_screenwidth() // 2, 50,
            text="Drag to select the region to watch. Press ESC to cancel.",
            fill="white", font=("Consolas", 16, "bold"),
        )

    def _on_press(self, event):
        self.start_x = event.x
        self.start_y = event.y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline="lime", width=2,
        )

    def _on_drag(self, event):
        self.canvas.coords(self.rect, self.start_x, self.start_y, event.x, event.y)

    def _on_release(self, event):
        x1 = min(self.start_x, event.x)
        y1 = min(self.start_y, event.y)
        x2 = max(self.start_x, event.x)
        y2 = max(self.start_y, event.y)
        w = x2 - x1
        h = y2 - y1
        self.root.destroy()
        if w > 5 and h > 5:
            self.on_selected({"left": x1, "top": y1, "width": w, "height": h})


# ══════════════════════════════════════════════════════════════
#  Macro Recorder
# ══════════════════════════════════════════════════════════════

class WindowHelper:
    """Helpers to find, focus, and restore windows using Win32 API."""

    @staticmethod
    def get_foreground_window():
        return ctypes.windll.user32.GetForegroundWindow()

    @staticmethod
    def get_window_title(hwnd):
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    @staticmethod
    def focus_window(hwnd):
        """Bring a window to the foreground."""
        if not hwnd:
            return False
        try:
            # If minimized, restore it
            SW_RESTORE = 9
            if ctypes.windll.user32.IsIconic(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
            # Allow our process to set foreground window
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            time.sleep(0.3)
            return True
        except Exception:
            return False


class MacroRecorder:
    def __init__(self):
        self.events = []
        self.recording = False
        self.playing = False
        self.paused = False
        self.start_time = 0
        self.loop_count = 1
        self.playback_speed = 1.0
        self.target_window = None  # hwnd of the game window
        self.target_window_title = ""
        self.mouse_controller = MouseController()
        self.keyboard_controller = KeyboardController()
        self._play_thread = None
        self._stop_playback = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._mouse_listener = None
        self._keyboard_listener = None

    def start_recording(self):
        self.events = []
        self.recording = True
        self.start_time = time.perf_counter()
        # Remember which window is focused (the game window)
        self.target_window = WindowHelper.get_foreground_window()
        self.target_window_title = WindowHelper.get_window_title(self.target_window)
        self._mouse_listener = mouse.Listener(
            on_move=self._on_mouse_move,
            on_click=self._on_mouse_click,
            on_scroll=self._on_mouse_scroll,
        )
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self._mouse_listener.start()
        self._keyboard_listener.start()

    def stop_recording(self):
        self.recording = False
        if self._mouse_listener:
            self._mouse_listener.stop()
        if self._keyboard_listener:
            self._keyboard_listener.stop()

    def _elapsed(self):
        return time.perf_counter() - self.start_time

    def _on_mouse_move(self, x, y):
        if self.recording:
            self.events.append({"t": self._elapsed(), "type": "move", "x": x, "y": y})

    def _on_mouse_click(self, x, y, button, pressed):
        if self.recording:
            self.events.append({
                "t": self._elapsed(), "type": "click",
                "x": x, "y": y, "button": button.name, "pressed": pressed,
            })

    def _on_mouse_scroll(self, x, y, dx, dy):
        if self.recording:
            self.events.append({
                "t": self._elapsed(), "type": "scroll",
                "x": x, "y": y, "dx": dx, "dy": dy,
            })

    IGNORED_KEYS = {Key.f6, Key.f7, Key.f8, Key.f9}
    IGNORED_CHARS = {'v'}

    def _on_key_press(self, key):
        if self.recording and key not in self.IGNORED_KEYS and not (hasattr(key, 'char') and key.char in self.IGNORED_CHARS):
            self.events.append({
                "t": self._elapsed(), "type": "key_press",
                "key": self._serialize_key(key),
            })

    def _on_key_release(self, key):
        if self.recording and key not in self.IGNORED_KEYS and not (hasattr(key, 'char') and key.char in self.IGNORED_CHARS):
            self.events.append({
                "t": self._elapsed(), "type": "key_release",
                "key": self._serialize_key(key),
            })

    @staticmethod
    def _serialize_key(key):
        if isinstance(key, Key):
            return {"special": key.name}
        elif hasattr(key, "char") and key.char is not None:
            return {"char": key.char}
        else:
            return {"vk": key.vk if hasattr(key, "vk") else str(key)}

    @staticmethod
    def _deserialize_key(data):
        if "special" in data:
            return Key[data["special"]]
        elif "char" in data:
            from pynput.keyboard import KeyCode
            return KeyCode.from_char(data["char"])
        elif "vk" in data:
            from pynput.keyboard import KeyCode
            return KeyCode.from_vk(int(data["vk"]))
        return None

    @staticmethod
    def _deserialize_button(name):
        return MouseButton[name]

    def start_playback(self, on_loop_update=None, on_done=None):
        if not self.events:
            return
        self._stop_playback.clear()
        self._pause_event.set()
        self.playing = True
        self.paused = False

        def _play():
            self.focus_target()
            loops = self.loop_count if self.loop_count > 0 else float("inf")
            current = 0
            while current < loops and not self._stop_playback.is_set():
                current += 1
                if on_loop_update:
                    on_loop_update(current)
                self._replay_once()
                if self._stop_playback.is_set():
                    break
            self.playing = False
            self.paused = False
            if on_done:
                on_done()

        self._play_thread = threading.Thread(target=_play, daemon=True)
        self._play_thread.start()

    def focus_target(self):
        """Focus the game window that was active during recording."""
        if self.target_window:
            return WindowHelper.focus_window(self.target_window)
        return False

    def play_once_blocking(self):
        self._stop_playback.clear()
        self.playing = True
        self.focus_target()
        self._replay_once()
        self.playing = False
        return not self._stop_playback.is_set()

    def _replay_once(self):
        if not self.events:
            return
        start = time.perf_counter()
        for event in self.events:
            if self._stop_playback.is_set():
                return
            self._pause_event.wait()
            target_time = event["t"] / self.playback_speed
            now = time.perf_counter() - start
            wait = target_time - now
            if wait > 0:
                end = time.perf_counter() + wait
                while time.perf_counter() < end:
                    if self._stop_playback.is_set():
                        return
                    time.sleep(min(0.01, end - time.perf_counter()))
            self._execute_event(event)

    def _execute_event(self, event):
        t = event["type"]
        if t == "move":
            self.mouse_controller.position = (event["x"], event["y"])
        elif t == "click":
            btn = self._deserialize_button(event["button"])
            self.mouse_controller.position = (event["x"], event["y"])
            if event["pressed"]:
                self.mouse_controller.press(btn)
            else:
                self.mouse_controller.release(btn)
        elif t == "scroll":
            self.mouse_controller.position = (event["x"], event["y"])
            self.mouse_controller.scroll(event["dx"], event["dy"])
        elif t == "key_press":
            key = self._deserialize_key(event["key"])
            if key:
                self.keyboard_controller.press(key)
        elif t == "key_release":
            key = self._deserialize_key(event["key"])
            if key:
                self.keyboard_controller.release(key)

    def stop_playback(self):
        self._stop_playback.set()
        self._pause_event.set()
        self.playing = False
        self.paused = False

    def toggle_pause(self):
        if not self.playing:
            return
        if self.paused:
            self.paused = False
            self._pause_event.set()
        else:
            self.paused = True
            self._pause_event.clear()

    def save(self, path):
        with open(path, "w") as f:
            json.dump(self.events, f)

    def load(self, path):
        with open(path, "r") as f:
            self.events = json.load(f)


# ══════════════════════════════════════════════════════════════
#  Overlay UI
# ══════════════════════════════════════════════════════════════

class OverlayApp:
    def __init__(self):
        self.recorder = MacroRecorder()
        self.watcher = ScreenWatcher()
        self.battle_detector = BattleDetector()
        self.auto_farming = False
        self._auto_farm_thread = None
        self._stop_auto_farm = threading.Event()
        self.logger = None  # set after UI built

        self.root = tk.Tk()
        self.root.title("MK-Recorder")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e2e")
        self.root.attributes("-alpha", 0.92)

        self._build_ui()
        self._bind_hotkeys()
        self._update_preview()

    def _build_ui(self):
        style = {"bg": "#1e1e2e", "fg": "#cdd6f4", "font": ("Consolas", 10)}
        btn_style = {
            "bg": "#313244", "fg": "#cdd6f4", "activebackground": "#45475a",
            "activeforeground": "#cdd6f4", "font": ("Consolas", 10, "bold"),
            "relief": "flat", "cursor": "hand2", "padx": 8, "pady": 4,
        }

        # Status
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(self.root, textvariable=self.status_var, **style,
                 anchor="w", padx=6, pady=4).pack(fill="x")

        # Row 1: Record / Play / Pause
        row1 = tk.Frame(self.root, bg="#1e1e2e")
        row1.pack(fill="x", padx=4, pady=2)
        self.btn_record = tk.Button(row1, text="Record [F6]",
                                    command=self._toggle_record, **btn_style)
        self.btn_record.pack(side="left", expand=True, fill="x", padx=2)
        self.btn_play = tk.Button(row1, text="Play [F7]",
                                  command=self._toggle_play, **btn_style)
        self.btn_play.pack(side="left", expand=True, fill="x", padx=2)
        self.btn_pause = tk.Button(row1, text="Pause [F8]",
                                   command=self._toggle_pause, **btn_style)
        self.btn_pause.pack(side="left", expand=True, fill="x", padx=2)

        # Row 2: Save / Load / Exit
        row2 = tk.Frame(self.root, bg="#1e1e2e")
        row2.pack(fill="x", padx=4, pady=2)
        tk.Button(row2, text="Save", command=self._save, **btn_style
                  ).pack(side="left", expand=True, fill="x", padx=2)
        tk.Button(row2, text="Load", command=self._load, **btn_style
                  ).pack(side="left", expand=True, fill="x", padx=2)
        tk.Button(row2, text="Exit [F9]", command=self._exit, **btn_style
                  ).pack(side="left", expand=True, fill="x", padx=2)

        # Row 3: Loop / Speed
        row3 = tk.Frame(self.root, bg="#1e1e2e")
        row3.pack(fill="x", padx=4, pady=4)
        tk.Label(row3, text="Loops:", **style).pack(side="left")
        self.loop_var = tk.StringVar(value="1")
        tk.Entry(row3, textvariable=self.loop_var, width=5,
                 bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                 font=("Consolas", 10), relief="flat").pack(side="left", padx=4)
        tk.Label(row3, text="(0=inf)", **style).pack(side="left")
        tk.Label(row3, text="  Speed:", **style).pack(side="left")
        self.speed_var = tk.StringVar(value="1.0")
        tk.Entry(row3, textvariable=self.speed_var, width=5,
                 bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                 font=("Consolas", 10), relief="flat").pack(side="left", padx=4)
        tk.Label(row3, text="x", **style).pack(side="left")

        # Separator
        tk.Frame(self.root, bg="#45475a", height=2).pack(fill="x", padx=8, pady=4)

        # Screen Detection header
        tk.Label(self.root, text="-- Auto-Farm (Movement Detection) --", bg="#1e1e2e",
                 fg="#cdd6f4", font=("Consolas", 10, "bold")).pack()

        # Row 4: Select Region + Auto Farm
        row4 = tk.Frame(self.root, bg="#1e1e2e")
        row4.pack(fill="x", padx=4, pady=2)
        tk.Button(row4, text="Select Region",
                  command=self._select_region, **btn_style
                  ).pack(side="left", expand=True, fill="x", padx=2)
        self.btn_auto_farm = tk.Button(row4, text="Auto-Farm [V]",
                                       command=self._toggle_auto_farm, **btn_style)
        self.btn_auto_farm.pack(side="left", expand=True, fill="x", padx=2)

        # Row 5: Idle threshold + Idle time
        row5 = tk.Frame(self.root, bg="#1e1e2e")
        row5.pack(fill="x", padx=4, pady=2)
        tk.Label(row5, text="Idle if change <", **style).pack(side="left")
        self.idle_threshold_var = tk.StringVar(value="0.5")
        tk.Entry(row5, textvariable=self.idle_threshold_var, width=4,
                 bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                 font=("Consolas", 10), relief="flat").pack(side="left", padx=4)
        tk.Label(row5, text="%  for", **style).pack(side="left")
        self.idle_time_var = tk.StringVar(value="3.0")
        tk.Entry(row5, textvariable=self.idle_time_var, width=4,
                 bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                 font=("Consolas", 10), relief="flat").pack(side="left", padx=4)
        tk.Label(row5, text="sec", **style).pack(side="left")

        # Preview row
        row6 = tk.Frame(self.root, bg="#1e1e2e")
        row6.pack(fill="x", padx=4, pady=4)

        self.live_label = tk.Label(row6, text="No region", bg="#313244",
                                   fg="#6c7086", width=16, height=4,
                                   font=("Consolas", 8))
        self.live_label.pack(side="left", padx=4)

        self.change_var = tk.StringVar(value="Change: --")
        self.change_label = tk.Label(row6, textvariable=self.change_var, bg="#1e1e2e",
                                     fg="#cdd6f4", font=("Consolas", 12, "bold"))
        self.change_label.pack(side="left", padx=8)

        # Info bar
        self.info_var = tk.StringVar(value="Events: 0  |  Loop: -")
        tk.Label(self.root, textvariable=self.info_var, **style,
                 anchor="w", padx=6, pady=2).pack(fill="x")

        # Separator
        tk.Frame(self.root, bg="#45475a", height=2).pack(fill="x", padx=8, pady=4)

        # ── BATTLE ESCAPE ──
        tk.Label(self.root, text="-- Battle Escape (Auto-Run) --", bg="#1e1e2e",
                 fg="#cdd6f4", font=("Consolas", 10, "bold")).pack()

        row_battle1 = tk.Frame(self.root, bg="#1e1e2e")
        row_battle1.pack(fill="x", padx=4, pady=2)
        tk.Button(row_battle1, text="1. Select Run Btn",
                  command=self._select_run_button, **btn_style
                  ).pack(side="left", expand=True, fill="x", padx=2)
        tk.Button(row_battle1, text="2. Capture Run Btn",
                  command=self._capture_run_button, **btn_style
                  ).pack(side="left", expand=True, fill="x", padx=2)
        tk.Button(row_battle1, text="3. Test",
                  command=self._test_battle_detection, **btn_style
                  ).pack(side="left", expand=True, fill="x", padx=2)

        row_battle2 = tk.Frame(self.root, bg="#1e1e2e")
        row_battle2.pack(fill="x", padx=4, pady=2)
        self.battle_enabled_var = tk.BooleanVar(value=False)
        self.btn_battle_toggle = tk.Checkbutton(
            row_battle2, text="Enable Battle Escape",
            variable=self.battle_enabled_var,
            command=self._toggle_battle_escape,
            bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244",
            activebackground="#1e1e2e", activeforeground="#cdd6f4",
            font=("Consolas", 10),
        )
        self.btn_battle_toggle.pack(side="left", padx=4)

        tk.Label(row_battle2, text="Match:", **style).pack(side="left")
        self.battle_threshold_var = tk.StringVar(value="85")
        tk.Entry(row_battle2, textvariable=self.battle_threshold_var, width=3,
                 bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                 font=("Consolas", 10), relief="flat").pack(side="left", padx=2)
        tk.Label(row_battle2, text="%", **style).pack(side="left")

        # Battle preview
        row_battle3 = tk.Frame(self.root, bg="#1e1e2e")
        row_battle3.pack(fill="x", padx=4, pady=2)
        self.battle_preview_label = tk.Label(row_battle3, text="No capture", bg="#313244",
                                              fg="#6c7086", width=16, height=3,
                                              font=("Consolas", 8))
        self.battle_preview_label.pack(side="left", padx=4)
        self.battle_status_var = tk.StringVar(value="Battle: not configured")
        self.battle_status_label = tk.Label(row_battle3, textvariable=self.battle_status_var,
                                             bg="#1e1e2e", fg="#6c7086",
                                             font=("Consolas", 10))
        self.battle_status_label.pack(side="left", padx=8)

        # Separator
        tk.Frame(self.root, bg="#45475a", height=2).pack(fill="x", padx=8, pady=4)

        # ── DEBUG LOG PANEL ──
        tk.Label(self.root, text="-- Debug Log --", bg="#1e1e2e",
                 fg="#cdd6f4", font=("Consolas", 10, "bold")).pack()

        log_frame = tk.Frame(self.root, bg="#1e1e2e")
        log_frame.pack(fill="both", padx=4, pady=2)

        scrollbar = tk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")

        self.log_text = tk.Text(
            log_frame, height=10, width=55,
            bg="#11111b", fg="#cdd6f4", font=("Consolas", 9),
            state="disabled", wrap="word",
            yscrollcommand=scrollbar.set,
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.log_text.yview)

        # Color tags for log
        self.log_text.tag_config("info", foreground="#cdd6f4")
        self.log_text.tag_config("idle", foreground="#a6e3a1")     # green
        self.log_text.tag_config("active", foreground="#f38ba8")   # red
        self.log_text.tag_config("trigger", foreground="#f9e2af")  # yellow
        self.log_text.tag_config("macro", foreground="#89b4fa")    # blue
        self.log_text.tag_config("error", foreground="#f38ba8")

        # Clear log button
        tk.Button(self.root, text="Clear Log", command=self._clear_log, **btn_style
                  ).pack(fill="x", padx=4, pady=2)

        # Create logger
        self.logger = Logger(self.log_text)
        self.logger.log("MK-Recorder started", "info")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    # ── Live preview ───────────────────────────────────────────

    def _update_preview(self):
        if self.watcher.region and not self.auto_farming:
            try:
                current = self.watcher.capture_region()
                if current is not None:
                    img = Image.fromarray(current).resize((100, 60))
                    photo = ImageTk.PhotoImage(img)
                    self.live_label.config(image=photo, text="")
                    self.live_label._photo = photo

                    change = self.watcher.get_change_percent()
                    self.change_var.set(f"Change: {change}%")
                    try:
                        threshold = float(self.idle_threshold_var.get())
                    except ValueError:
                        threshold = 0.5
                    if change < threshold:
                        self.change_label.config(fg="#a6e3a1")
                    else:
                        self.change_label.config(fg="#f38ba8")
            except Exception:
                pass

        self.root.after(500, self._update_preview)

    # ── Hotkeys ────────────────────────────────────────────────

    def _bind_hotkeys(self):
        def on_press(key):
            if hasattr(key, 'char') and key.char == 'v':
                self.root.after(0, self._toggle_auto_farm)
            elif key == Key.f6:
                self.root.after(0, self._toggle_record)
            elif key == Key.f7:
                self.root.after(0, self._toggle_play)
            elif key == Key.f8:
                self.root.after(0, self._toggle_pause)
            elif key == Key.f9:
                self.root.after(0, self._exit)

        self._hotkey_listener = keyboard.Listener(on_press=on_press)
        self._hotkey_listener.daemon = True
        self._hotkey_listener.start()

    # ── Record ─────────────────────────────────────────────────

    def _toggle_record(self):
        if self.recorder.playing or self.auto_farming:
            return
        if self.recorder.recording:
            self.recorder.stop_recording()
            count = len(self.recorder.events)
            self.status_var.set(f"Stopped recording ({count} events)")
            self.info_var.set(f"Events: {count}  |  Loop: -")
            self.btn_record.config(text="Record [F6]", bg="#313244")
            self.logger.log(f"RECORDING STOPPED - {count} events captured", "info")
        else:
            self.btn_record.config(state="disabled")
            self.logger.log("RECORDING countdown started (3s)...", "info")
            self._record_countdown(3)

    def _record_countdown(self, seconds):
        if seconds > 0:
            self.status_var.set(f"Recording in {seconds}... (click on your game now!)")
            self.root.after(1000, self._record_countdown, seconds - 1)
        else:
            self.recorder.start_recording()
            self.status_var.set("Recording... (press F6 to stop)")
            self.btn_record.config(text="Stop Rec [F6]", bg="#f38ba8", state="normal")
            self.logger.log("RECORDING STARTED - capturing mouse & keyboard", "macro")
            self.logger.log(
                f"Target window saved: '{self.recorder.target_window_title}' "
                f"(hwnd={self.recorder.target_window})", "info"
            )

    # ── Play ───────────────────────────────────────────────────

    def _toggle_play(self):
        if self.recorder.recording or self.auto_farming:
            return
        if self.recorder.playing:
            self.recorder.stop_playback()
            self.status_var.set("Playback stopped")
            self.btn_play.config(text="Play [F7]", bg="#313244")
            self.logger.log("PLAYBACK STOPPED by user", "info")
        else:
            if not self.recorder.events:
                self.status_var.set("Nothing to play!")
                return
            self._apply_settings()
            self.status_var.set("Playing...")
            self.btn_play.config(text="Stop [F7]", bg="#a6e3a1")
            self.logger.log(f"PLAYBACK STARTED - {len(self.recorder.events)} events, speed={self.recorder.playback_speed}x", "macro")

            def on_loop(n):
                self.root.after(0, lambda: self.info_var.set(
                    f"Events: {len(self.recorder.events)}  |  Loop: {n}"))

            def on_done():
                self.root.after(0, lambda: (
                    self.status_var.set("Playback finished"),
                    self.btn_play.config(text="Play [F7]", bg="#313244"),
                ))
                self.logger.log("PLAYBACK FINISHED", "info")

            self.recorder.start_playback(on_loop_update=on_loop, on_done=on_done)

    def _toggle_pause(self):
        self.recorder.toggle_pause()
        if self.recorder.paused:
            self.status_var.set("Paused")
            self.btn_pause.config(bg="#f9e2af")
        else:
            self.status_var.set("Playing...")
            self.btn_pause.config(bg="#313244")

    def _apply_settings(self):
        try:
            self.recorder.loop_count = int(self.loop_var.get())
        except ValueError:
            self.recorder.loop_count = 1
        try:
            self.recorder.playback_speed = float(self.speed_var.get())
            if self.recorder.playback_speed <= 0:
                self.recorder.playback_speed = 1.0
        except ValueError:
            self.recorder.playback_speed = 1.0

    # ── Select Region ──────────────────────────────────────────

    def _select_region(self):
        self.root.withdraw()
        time.sleep(0.2)

        def on_selected(region):
            self.watcher.region = region
            self.watcher.reset()
            self.root.deiconify()
            self.status_var.set(
                f"Region set: ({region['left']},{region['top']}) "
                f"{region['width']}x{region['height']}"
            )
            self.logger.log(
                f"REGION SET: x={region['left']}, y={region['top']}, "
                f"w={region['width']}, h={region['height']}", "info"
            )

        def on_cancel():
            self.root.deiconify()

        selector = RegionSelector(on_selected)
        selector.root.protocol("WM_DELETE_WINDOW", lambda: (
            selector.root.destroy(), on_cancel()))

    # ── Battle Escape ──────────────────────────────────────────

    def _select_run_button(self):
        """Let user select where the Run button appears on screen."""
        self.root.withdraw()
        time.sleep(0.2)

        def on_selected(region):
            self.battle_detector.run_button_region = region
            self.root.deiconify()
            self.status_var.set(
                f"Run button region: ({region['left']},{region['top']}) "
                f"{region['width']}x{region['height']}"
            )
            self.logger.log(
                f"RUN BUTTON REGION SET: x={region['left']}, y={region['top']}, "
                f"w={region['width']}, h={region['height']}", "info"
            )

        def on_cancel():
            self.root.deiconify()

        selector = RegionSelector(on_selected)
        selector.root.protocol("WM_DELETE_WINDOW", lambda: (
            selector.root.destroy(), on_cancel()))

    def _capture_run_button(self):
        """Capture what the Run button looks like right now."""
        if not self.battle_detector.run_button_region:
            self.status_var.set("Select the Run button region first!")
            return
        img = self.battle_detector.capture_run_button()
        if img is not None:
            # Show preview
            pil_img = Image.fromarray(img).resize((100, 40))
            photo = ImageTk.PhotoImage(pil_img)
            self.battle_preview_label.config(image=photo, text="")
            self.battle_preview_label._photo = photo
            self.battle_status_var.set("Run btn captured!")
            self.logger.log("RUN BUTTON CAPTURED - battle escape ready", "info")
        else:
            self.status_var.set("Capture failed!")

    def _test_battle_detection(self):
        """Test if the Run button is detected right now."""
        if self.battle_detector.run_button_image is None:
            self.status_var.set("Capture the Run button first!")
            return
        sim, matched = self.battle_detector.get_similarity()
        if sim < 0:
            self.logger.log(f"TEST BATTLE: ERROR getting similarity", "error")
        elif matched:
            self.logger.log(f"TEST BATTLE: MATCH! Similarity={sim}% >= {self.battle_detector.match_threshold}%", "trigger")
            self.battle_status_var.set(f"TEST: MATCH {sim}%")
            self.battle_status_label.config(fg="#a6e3a1")
        else:
            self.logger.log(f"TEST BATTLE: NO match. Similarity={sim}% < {self.battle_detector.match_threshold}%", "active")
            self.battle_status_var.set(f"TEST: NO {sim}%")
            self.battle_status_label.config(fg="#f38ba8")

    def _toggle_battle_escape(self):
        enabled = self.battle_enabled_var.get()
        if enabled and self.battle_detector.run_button_image is None:
            self.battle_enabled_var.set(False)
            self.status_var.set("Capture the Run button first!")
            return
        self.battle_detector.enabled = enabled
        try:
            self.battle_detector.match_threshold = float(self.battle_threshold_var.get())
        except ValueError:
            self.battle_detector.match_threshold = 85
        if enabled:
            self.logger.log("BATTLE ESCAPE ENABLED", "info")
            self.battle_status_var.set("Battle: ARMED")
            self.battle_status_label.config(fg="#a6e3a1")
        else:
            self.logger.log("BATTLE ESCAPE DISABLED", "info")
            self.battle_status_var.set("Battle: disabled")
            self.battle_status_label.config(fg="#6c7086")

    # ── Auto-Farm ──────────────────────────────────────────────

    def _toggle_auto_farm(self):
        if self.auto_farming:
            self._stop_auto_farming()
        else:
            self._start_auto_farming()

    def _start_auto_farming(self):
        if not self.recorder.events:
            self.status_var.set("Record a macro first!")
            self.logger.log("AUTO-FARM FAILED: No macro recorded!", "error")
            return
        if not self.watcher.region:
            self.status_var.set("Select a screen region first!")
            self.logger.log("AUTO-FARM FAILED: No region selected!", "error")
            return

        self._apply_settings()
        try:
            idle_threshold = float(self.idle_threshold_var.get())
        except ValueError:
            idle_threshold = 0.5
        try:
            idle_time_needed = float(self.idle_time_var.get())
        except ValueError:
            idle_time_needed = 3.0

        self.auto_farming = True
        self._stop_auto_farm.clear()
        self.watcher.reset()
        self.btn_auto_farm.config(text="Stop Farm [V]", bg="#f9e2af")
        self.status_var.set("Auto-Farm: watching for idle...")

        self.logger.log("=" * 50, "info")
        self.logger.log(f"AUTO-FARM STARTED", "trigger")
        self.logger.log(f"  Idle threshold: < {idle_threshold}%", "info")
        self.logger.log(f"  Idle time needed: {idle_time_needed}s", "info")
        self.logger.log(f"  Macro events: {len(self.recorder.events)}", "info")
        self.logger.log(f"  Region: {self.watcher.region}", "info")
        self.logger.log("=" * 50, "info")

        def _farm_loop():
          try:
            cycle = 0
            idle_start = None
            check_num = 0

            self.logger.log("Farm loop thread started OK", "info")

            while not self._stop_auto_farm.is_set():
                check_num += 1

                # ── Battle check (before idle check) ──
                if self.battle_detector.enabled:
                    try:
                        battle_sim, in_battle = self.battle_detector.get_similarity()
                        if check_num % 5 == 0:
                            self.logger.log(
                                f"BATTLE CHECK #{check_num}: similarity={battle_sim}% "
                                f"(need >= {self.battle_detector.match_threshold}%) "
                                f"-> {'BATTLE!' if in_battle else 'no battle'}",
                                "trigger" if in_battle else "info"
                            )
                        if in_battle:
                            self.logger.log("!" * 50, "error")
                            self.logger.log("BATTLE DETECTED! Clicking Run button...", "error")
                            self.logger.log("!" * 50, "error")
                            self.root.after(0, lambda: (
                                self.status_var.set("BATTLE! Clicking Run..."),
                                self.battle_status_var.set("Battle: ESCAPING!"),
                                self.battle_status_label.config(fg="#f38ba8"),
                            ))

                            # Focus game window first, then click Run
                            self.recorder.focus_target()
                            time.sleep(0.3)
                            self.battle_detector.click_run_button()
                            self.logger.log("Clicked Run button!", "macro")

                            # Wait for battle to end
                            time.sleep(2.0)

                            # Check if still in battle (might need to click again)
                            for retry in range(5):
                                if self._stop_auto_farm.is_set():
                                    break
                                if self.battle_detector.is_battle():
                                    self.logger.log(f"Still in battle, clicking Run again (retry {retry+1})...", "error")
                                    self.battle_detector.click_run_button()
                                    time.sleep(2.0)
                                else:
                                    break

                            self.logger.log("Battle escaped! Resuming farm watch...", "info")
                            self.root.after(0, lambda: (
                                self.battle_status_var.set("Battle: ARMED"),
                                self.battle_status_label.config(fg="#a6e3a1"),
                            ))
                            idle_start = None
                            self.watcher.reset()
                            time.sleep(1.0)
                            self.watcher.reset()
                            continue
                    except Exception as e:
                        self.logger.log(f"Battle check error: {e}", "error")

                # ── Idle check ──
                change = self.watcher.get_change_percent()

                is_idle = change < idle_threshold

                if is_idle:
                    if idle_start is None:
                        idle_start = time.perf_counter()
                        self.logger.log(
                            f"CHECK #{check_num}: change={change}% -> IDLE START (< {idle_threshold}%)",
                            "idle"
                        )
                    idle_duration = time.perf_counter() - idle_start

                    # Log every few checks while idle
                    if check_num % 3 == 0:
                        self.logger.log(
                            f"CHECK #{check_num}: change={change}% -> STILL IDLE "
                            f"({idle_duration:.1f}s / {idle_time_needed}s needed)",
                            "idle"
                        )

                    self.root.after(0, lambda c=change, d=idle_duration: (
                        self.status_var.set(
                            f"Auto-Farm: IDLE ({c}%) for {d:.1f}s / {idle_time_needed}s"
                        ),
                        self.change_var.set(f"Change: {c}%"),
                        self.change_label.config(fg="#a6e3a1"),
                    ))

                    # TRIGGER: idle long enough!
                    if idle_duration >= idle_time_needed:
                        cycle += 1
                        self.logger.log("=" * 50, "trigger")
                        self.logger.log(
                            f">>> TRIGGERED! Idle for {idle_duration:.1f}s >= {idle_time_needed}s",
                            "trigger"
                        )
                        self.logger.log(
                            f">>> RUNNING MACRO (cycle #{cycle}) - {len(self.recorder.events)} events",
                            "trigger"
                        )
                        self.logger.log("=" * 50, "trigger")
                        self.logger.log(
                            f"Focusing game window: '{self.recorder.target_window_title}'", "info"
                        )

                        self.root.after(0, lambda n=cycle: (
                            self.status_var.set(f"Auto-Farm: TRIGGERED! Running macro (#{n})..."),
                            self.info_var.set(f"Events: {len(self.recorder.events)}  |  Farm cycle: {n}"),
                        ))

                        # Play macro (focus_target is called inside play_once_blocking)
                        macro_start = time.perf_counter()
                        self.recorder.play_once_blocking()
                        macro_duration = time.perf_counter() - macro_start

                        self.logger.log(
                            f"<<< MACRO FINISHED (took {macro_duration:.1f}s)", "macro"
                        )

                        idle_start = None
                        self.watcher.reset()

                        if self._stop_auto_farm.is_set():
                            break

                        self.logger.log("Waiting 1s before watching again...", "info")
                        time.sleep(1.0)
                        self.watcher.reset()  # reset again after wait
                        self.root.after(0, lambda: self.status_var.set(
                            "Auto-Farm: watching for idle..."))
                        self.logger.log("Resumed watching for idle...", "info")
                else:
                    # Screen is changing = character is active
                    if idle_start is not None:
                        lost_duration = time.perf_counter() - idle_start
                        self.logger.log(
                            f"CHECK #{check_num}: change={change}% -> ACTIVE AGAIN "
                            f"(was idle for {lost_duration:.1f}s, reset timer)",
                            "active"
                        )
                    idle_start = None

                    # Log active state periodically
                    if check_num % 5 == 0:
                        self.logger.log(
                            f"CHECK #{check_num}: change={change}% -> ACTIVE (>= {idle_threshold}%)",
                            "active"
                        )

                    self.root.after(0, lambda c=change: (
                        self.status_var.set(f"Auto-Farm: Active ({c}% change)"),
                        self.change_var.set(f"Change: {c}%"),
                        self.change_label.config(fg="#f38ba8"),
                    ))

                # Check every 0.5 seconds
                end = time.perf_counter() + 0.5
                while time.perf_counter() < end:
                    if self._stop_auto_farm.is_set():
                        break
                    time.sleep(0.05)

            self.auto_farming = False
            self.logger.log("AUTO-FARM STOPPED", "info")
            self.root.after(0, lambda: (
                self.btn_auto_farm.config(text="Auto-Farm [V]", bg="#313244"),
                self.status_var.set("Auto-Farm stopped"),
            ))
          except Exception as e:
            import traceback
            self.logger.log(f"FARM LOOP CRASHED: {e}", "error")
            self.logger.log(traceback.format_exc(), "error")
            self.auto_farming = False
            self.root.after(0, lambda: (
                self.btn_auto_farm.config(text="Auto-Farm [V]", bg="#313244"),
                self.status_var.set(f"Auto-Farm ERROR: {e}"),
            ))

        self._auto_farm_thread = threading.Thread(target=_farm_loop, daemon=True)
        self._auto_farm_thread.start()

    def _stop_auto_farming(self):
        self._stop_auto_farm.set()
        self.recorder.stop_playback()
        self.auto_farming = False
        self.btn_auto_farm.config(text="Auto-Farm [V]", bg="#313244")
        self.status_var.set("Auto-Farm stopped")

    # ── Save / Load / Exit ─────────────────────────────────────

    def _save(self):
        if not self.recorder.events:
            self.status_var.set("Nothing to save!")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Macro files", "*.json"), ("All files", "*.*")],
            initialdir=os.path.dirname(__file__),
        )
        if path:
            self.recorder.save(path)
            self.status_var.set(f"Saved: {os.path.basename(path)}")
            self.logger.log(f"SAVED macro to {path}", "info")

    def _load(self):
        path = filedialog.askopenfilename(
            filetypes=[("Macro files", "*.json"), ("All files", "*.*")],
            initialdir=os.path.dirname(__file__),
        )
        if path:
            self.recorder.load(path)
            count = len(self.recorder.events)
            self.status_var.set(f"Loaded: {os.path.basename(path)}")
            self.info_var.set(f"Events: {count}  |  Loop: -")
            self.logger.log(f"LOADED macro from {path} ({count} events)", "info")

    def _exit(self):
        if self.auto_farming:
            self._stop_auto_farming()
        if self.recorder.recording:
            self.recorder.stop_recording()
        if self.recorder.playing:
            self.recorder.stop_playback()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = OverlayApp()
    app.run()
