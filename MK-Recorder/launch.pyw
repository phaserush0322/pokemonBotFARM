"""Launcher that shows errors if the app crashes."""
import traceback
import sys
import os
import tkinter as tk

# Run mk_recorder.py directly so no __pycache__ is created
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from mk_recorder import OverlayApp
    app = OverlayApp()
    app.run()
except Exception as e:
    root = tk.Tk()
    root.withdraw()
    import tkinter.messagebox as mb
    mb.showerror("MK-Recorder Error", f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")
    root.destroy()
