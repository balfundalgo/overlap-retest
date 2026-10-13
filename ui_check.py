#!/usr/bin/env python3
"""
The Overlap — interface construction check
══════════════════════════════════════════
Builds the whole settings panel with the toolkit stubbed out.

This exists because of a crash that reached a client: the per-instrument
session rows were built before the instrument tick-boxes, so the panel read
`index_vars` a moment before it was created and the application died on
startup with `'_tkinter.tkapp' object has no attribute 'index_vars'`.

Nothing in the suite caught it, because every other test imports the modules
and parses them but never actually constructs the window. A real display is
not available here, so the toolkit is replaced with recording stubs and the
genuine OverlapApp build runs against them — same method order, same
attribute access, no Tk.

    python ui_check.py
"""

import sys
import types


class _Var:
    def __init__(self, value=None, **kw):
        self._v = value if value is not None else ""

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class _BoolVar(_Var):
    def __init__(self, value=False, **kw):
        self._v = bool(value)


class _W:
    """Stands in for every widget. Accepts anything, records nothing."""

    def __init__(self, *a, **k):
        self._children = []
        self._packed = False

    def __getattr__(self, name):
        def _any(*a, **k):
            return _W()
        return _any

    def pack(self, *a, **k):
        self._packed = True

    def pack_forget(self, *a, **k):
        self._packed = False

    def grid(self, *a, **k):
        self._packed = True

    def place(self, *a, **k):
        self._packed = True

    def configure(self, *a, **k):
        pass

    def cget(self, k):
        return ""

    def winfo_children(self):
        return list(self._children)

    def winfo_class(self):
        return "Frame"

    def destroy(self):
        pass

    def bind(self, *a, **k):
        pass


def _make_stub():
    m = types.ModuleType("customtkinter")
    for name in ("CTk", "CTkToplevel", "CTkFrame", "CTkLabel", "CTkButton",
                 "CTkEntry", "CTkOptionMenu", "CTkSwitch", "CTkCheckBox",
                 "CTkTextbox", "CTkScrollableFrame", "CTkProgressBar",
                 "CTkTabview", "CTkSegmentedButton", "CTkComboBox",
                 "CTkSlider", "CTkCanvas", "CTkImage", "CTkFont"):
        setattr(m, name, type(name, (_W,), {}))
    m.StringVar = _Var
    m.BooleanVar = _BoolVar
    m.IntVar = _Var
    m.DoubleVar = _Var
    m.set_appearance_mode = lambda *a, **k: None
    m.set_default_color_theme = lambda *a, **k: None
    m.set_widget_scaling = lambda *a, **k: None

    tk = types.ModuleType("tkinter")
    tk.StringVar, tk.BooleanVar, tk.IntVar, tk.DoubleVar = _Var, _BoolVar, _Var, _Var
    tk.TclError = Exception
    tk.END = "end"
    tk.Canvas = _W
    tk.messagebox = types.ModuleType("tkinter.messagebox")
    tk.messagebox.showerror = lambda *a, **k: None
    tk.messagebox.showinfo = lambda *a, **k: None
    tk.messagebox.askyesno = lambda *a, **k: True
    return m, tk


def main() -> int:
    ctk_stub, tk_stub = _make_stub()
    sys.modules["customtkinter"] = ctk_stub
    sys.modules["tkinter"] = tk_stub
    sys.modules["tkinter.messagebox"] = tk_stub.messagebox

    import app as appmod
    from config import INDEX_CONFIG

    fails = []

    def ck(name, ok, detail=""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    print("Building the settings panel with the toolkit stubbed...\n")

    try:
        a = appmod.OverlapApp()
    except Exception as e:
        ck("the window constructs", False, f"{type(e).__name__}: {e}")
        print("\n  This is the crash a client saw on startup.")
        return 1

    ck("the window constructs", True)
    ck("the instrument tick-boxes exist",
       bool(getattr(a, "index_vars", None)),
       ", ".join(getattr(a, "index_vars", {})))
    ck("every configured instrument has a tick-box",
       set(getattr(a, "index_vars", {})) == set(INDEX_CONFIG))
    ck("every instrument has a session row",
       set(getattr(a, "per_index_vars", {})) == set(INDEX_CONFIG),
       ", ".join(getattr(a, "per_index_vars", {})))
    ck("each session row has all four inputs",
       all(set(v) == {"timeframe", "ref_time", "last_entry", "square_off"}
           for v in getattr(a, "per_index_vars", {}).values()))

    try:
        cfg = a._collect_config()
        ck("the settings can be collected", True)
        ck("it defaults to one instrument", cfg.indices == ["NIFTY"], str(cfg.indices))
    except Exception as e:
        ck("the settings can be collected", False, f"{type(e).__name__}: {e}")
        return 1

    # ticking a second instrument must flow through
    try:
        a.index_vars["CRUDEOIL"].set(True)
        a._on_index_toggle()
        cfg = a._collect_config()
        ck("ticking crude selects it", "CRUDEOIL" in cfg.indices, str(cfg.indices))
        ck("its session is collected", "CRUDEOIL" in (cfg.per_index or {}),
           str((cfg.per_index or {}).get("CRUDEOIL")))
        sub = cfg.for_index("CRUDEOIL")
        ck("and resolves to 5-minute candles", sub.timeframe_minutes == 5)
        ck("while NIFTY stays on 3", cfg.for_index("NIFTY").timeframe_minutes == 3)
    except Exception as e:
        ck("ticking crude works", False, f"{type(e).__name__}: {e}")

    print()
    if fails:
        print(f"  INTERFACE CHECK FAILED — {len(fails)} problem(s)")
        return 1
    print("  INTERFACE CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
