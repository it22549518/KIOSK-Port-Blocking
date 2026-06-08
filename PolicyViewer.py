#!/usr/bin/env python3
r"""
Commercial Bank of Ceylon PLC
Policy Acceptance Kiosk Viewer  v3.0
======================================
- Displays 16 screenshot images (1.png to 16.png) fullscreen
- Tracks exact read time (start → accept)
- Logs to SQLite DB: username, pc, IP, domain, start_time, end_time, read_duration_seconds
- Blocks all system hotkeys (Alt+F4, Win, Alt+Tab, Escape, etc.)
- Accept button only on page 16
- Exits code 0 on accept, code 1 if somehow closed

Usage (called by PolicyLogon.ps1):
    python PolicyViewer.py --images-dir "SERVER/Share/images" \
                           --db "\\SERVER\Share\policy_log.db" \
                           --user "jdoe" --pc "PC001" --domain "CBC"

Dependencies:
    pip install Pillow
    (No PyMuPDF needed — uses PNG screenshots)
"""

import sys, os, argparse, datetime, socket, threading, time, sqlite3, tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk

# ─── WINDOWS KEYBOARD HOOK ────────────────────────────────────────────────────
WINDOWS = sys.platform == "win32"
if WINDOWS:
    import ctypes, ctypes.wintypes
    WH_KEYBOARD_LL = 13
    WM_KEYDOWN     = 0x0100
    WM_SYSKEYDOWN  = 0x0104
    BLOCKED_VK     = {0x09,0x1B,0x5B,0x5C,0x5D,0x74,0x75,0x76,0x77,
                      0x78,0x79,0x7A,0x7B,0x2C,0x46}  # Tab,Esc,Win,F5-F12,PrtScr,F
    LLKBProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int,
                                   ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)
    _hook_id = None; _hook_proc_ref = None

    def _kb_proc(nCode, wParam, lParam):
        if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            vk = ctypes.cast(lParam, ctypes.POINTER(ctypes.c_ulong))[0]
            if vk in BLOCKED_VK:
                return 1
        return ctypes.windll.user32.CallNextHookEx(_hook_id, nCode, wParam, lParam)

    def install_hook():
        global _hook_id, _hook_proc_ref
        _hook_proc_ref = LLKBProc(_kb_proc)
        _hook_id = ctypes.windll.user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, _hook_proc_ref,
            ctypes.windll.kernel32.GetModuleHandleW(None), 0)

    def remove_hook():
        global _hook_id
        if _hook_id:
            ctypes.windll.user32.UnhookWindowsHookEx(_hook_id)
            _hook_id = None

    def pump():
        msg = ctypes.wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
else:
    def install_hook(): pass
    def remove_hook():  pass
    def pump():         pass


# ─── MONITOR MANAGEMENT ──────────────────────────────────────────────────────
def get_monitor_count():
    """Return number of active monitors."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(80)  # SM_CMONITORS = 80
    except Exception:
        return 1

def disable_secondary_monitors():
    """Black out all secondary monitors, keep only primary."""
    if not WINDOWS:
        return False
    count = get_monitor_count()
    if count > 1:
        import subprocess
        subprocess.Popen(["DisplaySwitch.exe", "/internal"])
        import time; time.sleep(2)
        return True
    return False

def enable_all_monitors():
    """Restore extended desktop (all monitors)."""
    if not WINDOWS:
        return
    import subprocess
    subprocess.Popen(["DisplaySwitch.exe", "/extend"])
    import time; time.sleep(1)

def create_black_windows():
    """
    Create black fullscreen Tkinter windows on all secondary monitors.
    More reliable than DisplaySwitch in some environments.
    Returns list of Toplevel windows created.
    """
    blacks = []
    try:
        import tkinter as tk
        Add_Type_done = False
        try:
            import ctypes
            # Get monitor count
            count = ctypes.windll.user32.GetSystemMetrics(80)
        except Exception:
            count = 1

        if count <= 1:
            return blacks

        # Use EnumDisplayMonitors to get each monitor's rect
        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(ctypes.wintypes.RECT),
            ctypes.c_double
        )
        monitors = []
        def callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
            r = lprcMonitor.contents
            monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return True
        ctypes.windll.user32.EnumDisplayMonitors(
            None, None, MonitorEnumProc(callback), 0
        )

        # Primary monitor is usually at (0,0)
        # Create black window on every monitor that is NOT (0,0)
        for (x, y, w, h) in monitors:
            if x == 0 and y == 0:
                continue   # skip primary
            win = tk.Toplevel()
            win.configure(bg="black")
            win.geometry(f"{w}x{h}+{x}+{y}")
            win.attributes("-fullscreen", True)
            win.attributes("-topmost", True)
            win.overrideredirect(True)
            win.focus_force()
            # Show "Please wait" message
            tk.Label(
                win,
                text="Please complete the policy acceptance\non the primary screen.",
                bg="black", fg="#333333",
                font=("Segoe UI", 14)
            ).place(relx=0.5, rely=0.5, anchor="center")
            win.update()
            blacks.append(win)
    except Exception as e:
        print(f"Black window error: {e}")
    return blacks

# ─── COLOURS ─────────────────────────────────────────────────────────────────
BG        = "#0a1628"
ACCENT    = "#c8960c"
BTN_BG    = "#1a2e4a"
BTN_HOV   = "#243d63"
FG        = "#e8e8e8"
NAV_H     = 80
HDR_H     = 54
TOTAL_PGS = 16

# ─── DATABASE ─────────────────────────────────────────────────────────────────
class PolicyDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_db()

    def _connect(self):
        # timeout=10 handles concurrent AD logons writing at same time
        return sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)

    def _ensure_db(self):
        """Create table if it doesn't exist."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS policy_acceptance (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    username            TEXT    NOT NULL,
                    pc_name             TEXT    NOT NULL,
                    domain              TEXT,
                    ip_address          TEXT,
                    start_time          TEXT    NOT NULL,
                    end_time            TEXT    NOT NULL,
                    read_duration_sec   INTEGER NOT NULL,
                    policy_version      TEXT    DEFAULT 'v1.0',
                    accepted            INTEGER DEFAULT 1
                )
            """)
            con.commit()

    def already_accepted(self, username: str, pc_name: str) -> bool:
        """Returns True if this user+PC already has an acceptance record."""
        with self._connect() as con:
            row = con.execute(
                "SELECT id FROM policy_acceptance WHERE username=? AND pc_name=? AND accepted=1",
                (username, pc_name)
            ).fetchone()
        return row is not None

    def save_acceptance(self, username, pc_name, domain, ip,
                        start_dt, end_dt, duration_sec, policy_version="v1.0"):
        """Write acceptance record. Retries on lock."""
        record = (username, pc_name, domain, ip,
                  start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                  end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                  int(duration_sec), policy_version)
        for attempt in range(8):
            try:
                with self._connect() as con:
                    con.execute("""
                        INSERT INTO policy_acceptance
                        (username,pc_name,domain,ip_address,
                         start_time,end_time,read_duration_sec,policy_version)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, record)
                    con.commit()
                return True
            except sqlite3.OperationalError:
                time.sleep(0.3 * (attempt + 1))
        return False


# ─── IMAGE LOADER ─────────────────────────────────────────────────────────────
class SlideImages:
    """Loads 1.png … 16.png from images_dir, caches resized PhotoImages."""
    def __init__(self, images_dir: str):
        self.dir   = images_dir
        self._raw  = {}   # page_num -> PIL Image
        self._cache = {}  # (page_num, w, h) -> PhotoImage
        self._load_all()

    def _load_all(self):
        missing = []
        for i in range(1, TOTAL_PGS + 1):
            path = os.path.join(self.dir, f"{i}.png")
            if not os.path.exists(path):
                # also try .jpg
                path = os.path.join(self.dir, f"{i}.jpg")
            if os.path.exists(path):
                self._raw[i] = Image.open(path).convert("RGB")
            else:
                missing.append(i)
        if missing:
            raise FileNotFoundError(
                f"Missing slide images: {missing}\n"
                f"Expected files 1.png–16.png in: {self.dir}"
            )

    def get(self, page_num: int, width: int, height: int) -> ImageTk.PhotoImage:
        key = (page_num, width, height)
        if key not in self._cache:
            img = self._raw[page_num].copy()
            img.thumbnail((width, height), Image.LANCZOS)
            self._cache[key] = ImageTk.PhotoImage(img)
        return self._cache[key]

    @property
    def count(self):
        return len(self._raw)


# ─── MAIN APP ─────────────────────────────────────────────────────────────────
class PolicyViewer:
    def __init__(self, images_dir, db_path, username, pc_name, domain,
                 accept_flag, policy_version):
        self.images_dir     = images_dir
        self.db_path        = db_path
        self.username       = username
        self.pc_name        = pc_name
        self.domain         = domain
        self.accept_flag    = accept_flag
        self.policy_version = policy_version
        self.current        = 0
        self.accepted       = False
        self.start_time     = datetime.datetime.now()

        # Resolve best IPv4 — prefer real LAN, skip loopback/virtual adapters
        self.ip = self._get_best_ip()

        # Lock secondary monitors
        self._had_dual = disable_secondary_monitors()
        # Also create black overlay windows as backup
        self._black_wins = []   # filled after root exists

        self._build_window()
        self._load_slides()
        self._build_ui()
        # Black overlay on secondary monitors (created after root window exists)
        self._black_wins = create_black_windows()
        # Delay first render 200ms so window has real pixel dimensions
        self.root.after(200, lambda: self._show_page(0))

    def _get_best_ip(self) -> str:
        candidates = []
        # UDP connect trick — picks the interface used for real traffic
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            candidates.insert(0, s.getsockname()[0])
            s.close()
        except Exception:
            pass
        # Also gather all IPv4s from hostname
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            for info in infos:
                ip = info[4][0]
                if ip not in candidates:
                    candidates.append(ip)
        except Exception:
            pass
        # Skip loopback and known virtual adapter ranges (VMware, VirtualBox)
        SKIP = ("127.", "169.254.", "192.168.247.", "192.168.56.")
        good = [ip for ip in candidates if not any(ip.startswith(p) for p in SKIP)]
        if good:
            return good[0]
        elif candidates:
            return candidates[0]
        return "Unknown"

    # ── WINDOW ────────────────────────────────────────────────────────────────
    def _build_window(self):
        self.root = tk.Tk()
        self.root.title("CBC – Policy Acceptance")
        self.root.configure(bg=BG)
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.focus_force()
        self.root.grab_set_global()
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)  # block X button
        # Block keys at Tk level (double-safe with hook)
        for seq in ("<Escape>","<Alt-F4>","<Alt-Tab>","<Super_L>","<Super_R>",
                    "<Control-Escape>","<F1>","<F5>"):
            self.root.bind(seq, lambda e: "break")
        self.sw = self.root.winfo_screenwidth()
        self.sh = self.root.winfo_screenheight()

    # ── SLIDES ────────────────────────────────────────────────────────────────
    def _load_slides(self):
        try:
            self.slides = SlideImages(self.images_dir)
        except FileNotFoundError as e:
            self._fatal(str(e))

    def _fatal(self, msg):
        remove_hook()
        self.root.grab_release()
        tk.messagebox.showerror("CBC Policy Viewer – Error", msg)
        self.root.destroy()
        sys.exit(2)

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        sw, sh = self.sw, self.sh

        # Header
        hdr = tk.Frame(self.root, bg=BG, height=HDR_H)
        hdr.pack(fill=tk.X); hdr.pack_propagate(False)
        tk.Label(hdr, text="  🏛  Commercial Bank of Ceylon PLC  –  Employee Policy",
                 bg=BG, fg=ACCENT, font=("Georgia", 13, "bold"), anchor="w"
                 ).pack(side=tk.LEFT, padx=16, pady=10)
        self.lbl_page = tk.Label(hdr, bg=BG, fg=FG,
                                  font=("Consolas", 11), anchor="e")
        self.lbl_page.pack(side=tk.RIGHT, padx=16)
        # Timer label (right side of header)
        self.lbl_timer = tk.Label(hdr, bg=BG, fg="#6688aa",
                                   font=("Consolas", 10), anchor="e")
        self.lbl_timer.pack(side=tk.RIGHT, padx=4)

        tk.Frame(self.root, bg=ACCENT, height=2).pack(fill=tk.X)

        # Canvas
        self.canvas = tk.Canvas(self.root, bg="#111c2d", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._img_ref = None

        tk.Frame(self.root, bg=ACCENT, height=2).pack(fill=tk.X)

        # Nav bar
        nav = tk.Frame(self.root, bg=BG, height=NAV_H)
        nav.pack(fill=tk.X); nav.pack_propagate(False)
        self._nav = nav

        bs = dict(bg=BTN_BG, fg=FG, activebackground=BTN_HOV, activeforeground=ACCENT,
                  relief=tk.FLAT, bd=0, font=("Consolas", 11, "bold"),
                  cursor="hand2", padx=22, pady=9)

        self.btn_prev = tk.Button(nav, text="◀  Previous", command=self._prev, **bs)
        self.btn_prev.pack(side=tk.LEFT, padx=(20,8), pady=14)

        # Progress centre
        cf = tk.Frame(nav, bg=BG); cf.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=16)
        self.prog_var = tk.DoubleVar()
        sty = ttk.Style(); sty.theme_use("clam")
        sty.configure("G.Horizontal.TProgressbar", troughcolor="#1a2e4a",
                       background=ACCENT, thickness=12, borderwidth=0)
        ttk.Progressbar(cf, variable=self.prog_var, style="G.Horizontal.TProgressbar",
                        maximum=100).pack(fill=tk.X, pady=(20,4))
        self.lbl_prog = tk.Label(cf, bg=BG, fg="#8899aa", font=("Consolas", 9))
        self.lbl_prog.pack()

        self.btn_next = tk.Button(nav, text="Next  ▶", command=self._next, **bs)
        self.btn_next.pack(side=tk.RIGHT, padx=(8,8), pady=14)

        # Accept button (hidden until page 16)
        self.btn_accept = tk.Button(
            nav, text="  ✔   I Have Read and I Accept This Policy  ",
            command=self._on_accept,
            bg="#1a5c2a", fg="#ffffff",
            activebackground="#22773a", activeforeground="#ffffff",
            relief=tk.FLAT, bd=0, font=("Georgia", 12, "bold"),
            cursor="hand2", padx=24, pady=9)

        # Mouse / keyboard navigation
        self.root.bind("<MouseWheel>", self._wheel)
        self.root.bind("<Button-4>",   lambda e: self._prev())
        self.root.bind("<Button-5>",   lambda e: self._next())
        self.root.bind("<Right>",      lambda e: self._next())
        self.root.bind("<Left>",       lambda e: self._prev())

        # Start elapsed-time ticker
        self._tick()

    # ── TIMER TICK ────────────────────────────────────────────────────────────
    def _tick(self):
        elapsed = int((datetime.datetime.now() - self.start_time).total_seconds())
        m, s = divmod(elapsed, 60)
        self.lbl_timer.config(text=f"⏱ Time: {m:02d}:{s:02d}   ")
        self._tick_job = self.root.after(1000, self._tick)

    # ── PAGE SHOW ─────────────────────────────────────────────────────────────
    def _show_page(self, n: int):
        self.current = max(0, min(n, TOTAL_PGS - 1))
        total = TOTAL_PGS

        self.lbl_page.config(text=f"Page {self.current+1} of {total}  ")

        # Get canvas real size
        self.root.update_idletasks(); self.root.update()
        cw = max(self.canvas.winfo_width(),  self.sw - 40)
        ch = max(self.canvas.winfo_height(), self.sh - HDR_H - 4 - NAV_H - 20)

        photo = self.slides.get(self.current + 1, cw - 20, ch - 10)
        self._img_ref = photo
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=photo, anchor=tk.CENTER)

        # Progress
        pct = ((self.current + 1) / total) * 100
        self.prog_var.set(pct)
        self.lbl_prog.config(text=f"Progress: {self.current+1}/{total} pages  ({int(pct)}%)")

        # Buttons
        self.btn_prev.config(state=tk.NORMAL if self.current > 0 else tk.DISABLED)

        if self.current == total - 1:         # last page
            self.btn_next.pack_forget()
            self.btn_accept.pack(side=tk.RIGHT, padx=(8,20), pady=14)
            self._pulse(True)
        else:
            self.btn_accept.pack_forget()
            self.btn_next.pack(side=tk.RIGHT, padx=(8,8), pady=14)
            self.btn_next.config(state=tk.NORMAL)
            self._pulse(False)

    def _next(self):
        if self.current < TOTAL_PGS - 1:
            self._show_page(self.current + 1)

    def _prev(self):
        if self.current > 0:
            self._show_page(self.current - 1)

    def _wheel(self, e):
        if e.delta > 0: self._prev()
        else:           self._next()

    # ── PULSE ANIMATION ───────────────────────────────────────────────────────
    _pulse_job = None; _ps = False

    def _pulse(self, start):
        if self._pulse_job:
            self.root.after_cancel(self._pulse_job); self._pulse_job = None
        if start: self._do_pulse()

    def _do_pulse(self):
        self._ps = not self._ps
        self.btn_accept.config(bg="#22773a" if self._ps else "#1a5c2a")
        self._pulse_job = self.root.after(650, self._do_pulse)

    # ── ACCEPT ────────────────────────────────────────────────────────────────
    def _on_accept(self):
        end_time     = datetime.datetime.now()
        duration_sec = int((end_time - self.start_time).total_seconds())

        # Stop timer
        if self._tick_job:
            self.root.after_cancel(self._tick_job)

        # Save to DB
        db_ok = False
        try:
            db = PolicyDB(self.db_path)
            db_ok = db.save_acceptance(
                username       = self.username,
                pc_name        = self.pc_name,
                domain         = self.domain,
                ip             = self.ip,
                start_dt       = self.start_time,
                end_dt         = end_time,
                duration_sec   = duration_sec,
                policy_version = self.policy_version
            )
        except Exception as ex:
            print(f"DB write error: {ex}")

        # Write flag file for PowerShell
        try:
            os.makedirs(os.path.dirname(self.accept_flag) or ".", exist_ok=True)
            with open(self.accept_flag, "w") as f:
                f.write(f"{self.username},{self.pc_name},{end_time.isoformat()}")
        except Exception as ex:
            print(f"Flag file error: {ex}")

        self.accepted = True

        # Thank-you overlay
        m, s = divmod(duration_sec, 60)
        ov = tk.Frame(self.root, bg="#0d2a14", bd=2, relief=tk.RIDGE)
        ov.place(relx=0.5, rely=0.5, anchor=tk.CENTER, width=580, height=230)
        tk.Label(ov, text="✔  Policy Accepted", bg="#0d2a14", fg="#4cde7a",
                 font=("Georgia", 22, "bold")).pack(pady=(28,6))
        tk.Label(ov, text=f"Thank you, {self.username}.\nYour acceptance has been recorded.",
                 bg="#0d2a14", fg=FG, font=("Consolas", 11), justify=tk.CENTER).pack()
        tk.Label(ov,
                 text=f"Read time: {m}m {s}s  |  IP: {self.ip}  |  {'Saved to DB ✔' if db_ok else 'DB write failed ✗'}",
                 bg="#0d2a14", fg="#6688aa", font=("Consolas", 9)).pack(pady=(10,0))

        self.root.after(3000, self._finish)

    def _finish(self):
        remove_hook()
        # Destroy black overlay windows
        for w in self._black_wins:
            try: w.destroy()
            except Exception: pass
        self._black_wins = []
        self.root.grab_release()
        self.root.destroy()
        # Restore all monitors
        if self._had_dual:
            enable_all_monitors()

    # ── RUN ───────────────────────────────────────────────────────────────────
    def run(self):
        install_hook()
        threading.Thread(target=pump, daemon=True).start()
        self.root.mainloop()
        return 0 if self.accepted else 1


# ─── ENTRY ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images-dir",     default="images",
                    help="Folder containing 1.png … 16.png")
    ap.add_argument("--db",             default="policy_log.db",
                    help="Path to SQLite DB file (can be a UNC network path)")
    ap.add_argument("--user",           default=os.environ.get("USERNAME","unknown"))
    ap.add_argument("--pc",             default=os.environ.get("COMPUTERNAME","unknown"))
    ap.add_argument("--domain",         default=os.environ.get("USERDOMAIN",""))
    ap.add_argument("--accept-flag",    default=r"C:\Temp\cbc_accepted.tmp")
    ap.add_argument("--policy-version", default="v1.0")
    args = ap.parse_args()

    if not os.path.isdir(args.images_dir):
        print(f"ERROR: images folder not found: {args.images_dir}")
        sys.exit(2)

    app = PolicyViewer(
        images_dir     = args.images_dir,
        db_path        = args.db,
        username       = args.user,
        pc_name        = args.pc,
        domain         = args.domain,
        accept_flag    = args.accept_flag,
        policy_version = args.policy_version,
    )
    sys.exit(app.run())

if __name__ == "__main__":
    main()
