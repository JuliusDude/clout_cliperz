"""Desktop controls for the same commands used by the unattended worker."""

import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .batch import parse_links
from .media import channel_handle, channel_id
from .storage import ClipperError, clean_stopped_downloads
from .workflows import CLOUT_CUTS, MOVIE_RECAP, WORKFLOWS, default_caption, ensure_workflow_folders, workflow_output


def launch(output):
    try:
        import tkinter as tk
        from tkinter import filedialog, font, messagebox, ttk
        root = tk.Tk()
    except (ImportError, RuntimeError) as error:
        raise ClipperError("Install Python with Tk support or use the links and follow commands.") from error
    except Exception as error:
        raise ClipperError("A desktop display is needed for Studio. The links and follow commands work in a terminal.") from error
    for content_mode in WORKFLOWS:
        ensure_workflow_folders(workflow_output(output, content_mode))

    root.title("Shorts Studio")
    root.geometry("760x640")
    root.minsize(630, 610)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    family = next((f for f in ("Inter", "DM Sans", "Open Sans", "Segoe UI", "Arial")
                   if f in font.families(root)), "TkDefaultFont")
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    paper, ink, muted, accent = "#f7f7f2", "#171b1b", "#51605e", "#284f46"
    style.configure(".", font=(family, 10), background=paper, foreground=ink)
    style.configure("Heading.TLabel", font=(family, 24, "bold"))
    style.configure("Quiet.TLabel", foreground=muted)
    style.configure("TButton", padding=(13, 7))
    style.configure("Primary.TButton", background=accent, foreground="white", padding=(17, 8))
    style.map("Primary.TButton", background=[("active", "#38665b"), ("disabled", "#75847e")])
    style.configure("TNotebook.Tab", padding=(18, 9))
    style.map("TNotebook.Tab", background=[("selected", "#e0ebe3")])
    panel = ttk.Frame(root, padding=(24, 18))
    panel.grid(sticky="nsew")
    panel.columnconfigure(0, weight=1)
    panel.rowconfigure(5, weight=1)

    header = ttk.Frame(panel)
    header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
    header.columnconfigure(0, weight=1)
    ttk.Label(header, text="Shorts Studio", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
    connect_button = ttk.Button(header, text="Connect YouTube")
    connect_button.grid(row=0, column=1)
    workflow = tk.StringVar(value=CLOUT_CUTS)
    workflows = ttk.Frame(header)
    workflows.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
    ttk.Label(workflows, text="Content mode").grid(row=0, column=0, padx=(0, 9))
    for column, (value, label) in enumerate(WORKFLOWS.items(), start=1):
        ttk.Radiobutton(workflows, text=label, value=value, variable=workflow).grid(row=0, column=column, padx=(0, 9))
    tabs = ttk.Notebook(panel)
    tabs.grid(row=1, column=0, sticky="ew")
    links_tab, follow_tab = ttk.Frame(tabs, padding=15), ttk.Frame(tabs, padding=15)
    for tab, label in ((links_tab, "Paste links"), (follow_tab, "Follow channel")):
        tab.columnconfigure(0, weight=1)
        tabs.add(tab, text=label)
    source_label = ttk.Label(links_tab)
    source_label.grid(row=0, column=0, sticky="w")
    urls = tk.Text(links_tab, height=4, wrap="word", font=(family, 11), relief="solid", borderwidth=1,
                   background="white", foreground=ink, padx=8, pady=6, undo=True)
    urls.grid(row=1, column=0, sticky="ew", pady=(6, 0))
    ttk.Label(follow_tab, text="YouTube channel URL, @handle, or channel ID").grid(row=0, column=0, sticky="w")
    channel = ttk.Entry(follow_tab)
    channel.grid(row=1, column=0, sticky="ew", pady=(6, 8))
    latest = tk.BooleanVar(value=False)
    ttk.Checkbutton(follow_tab, text="Include the most recent upload on the first scan",
                    variable=latest).grid(row=2, column=0, sticky="w")

    options = ttk.Frame(panel)
    options.grid(row=2, column=0, sticky="ew", pady=13)
    for i, label in enumerate(("Clip count", "Count applies to", "Captions", "Destination")):
        options.columnconfigure(i, weight=1)
        ttk.Label(options, text=label).grid(row=0, column=i, sticky="w")
    count = tk.StringVar(value="3")
    ttk.Spinbox(options, from_=1, to=20, width=5, textvariable=count).grid(row=1, column=0, sticky="ew", padx=(0, 9))

    def choice(column, values):
        box = ttk.Combobox(options, values=values, state="readonly", width=13)
        box.current(0)
        box.grid(row=1, column=column, sticky="ew", padx=(0, 9 if column < 3 else 0))
        return box

    scope = choice(1, ("Whole batch", "Each video"))
    captions = choice(2, ("Pop", "Classic", "Stream-pop"))
    visibility = choice(3, ("YouTube public", "YouTube unlisted", "YouTube private", "Save locally"))
    actions = ttk.Frame(panel)
    actions.grid(row=3, column=0, sticky="ew", pady=(0, 14))
    actions.columnconfigure(2, weight=1)
    start = ttk.Button(actions, style="Primary.TButton")
    start.grid(row=0, column=0)
    stop = ttk.Button(actions, text="Stop", state="disabled")
    stop.grid(row=0, column=1, padx=(8, 0))
    open_clips = ttk.Button(actions, text="Open clips folder")
    open_clips.grid(row=0, column=3)
    status = tk.StringVar(value="Ready")
    ttk.Label(panel, textvariable=status, style="Quiet.TLabel").grid(row=4, column=0, sticky="w", pady=(0, 5))
    log = tk.Text(panel, background="#172321", foreground="#dfede4", relief="flat", wrap="word",
                  font=("TkFixedFont", 10), height=6, padx=10, pady=8, state="disabled")
    log.grid(row=5, column=0, sticky="nsew")
    events = queue.Queue()
    process = None
    stopping = False

    def following():
        return tabs.select() == str(follow_tab)

    def mode_changed(*_):
        source_label.configure(text="Narrated recap video links" if workflow.get() == MOVIE_RECAP else "Creator video links")
        scope.configure(state="disabled" if following() else "readonly")
        if following():
            scope.set("Each new upload")
        elif scope.get() == "Each new upload":
            scope.current(0)
        start.configure(text="Start following" if following() else "Create Shorts")

    def log_line(line):
        log.configure(state="normal")
        log.insert("end", line)
        if int(log.index("end-1c").split(".")[0]) > 1200:
            log.delete("1.0", "101.0")
        log.see("end")
        log.configure(state="disabled")

    def spawn(arguments, stdin_text=""):
        nonlocal process, stopping
        if process is not None:
            return
        entry = Path(__file__).resolve().parents[1] / "shorts.py"
        state = workflow_output(output, workflow.get()) / ".state" if arguments[0] != "connect" else None
        proc = subprocess.Popen([sys.executable, "-u", str(entry), *arguments],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                start_new_session=os.name != "nt")
        process, stopping = proc, False
        start.configure(state="disabled")
        connect_button.configure(state="disabled")
        stop.configure(state="normal")
        status.set("Connecting YouTube" if arguments[0] == "connect" else "Running")

        def collect():
            try:
                with proc.stdin:
                    proc.stdin.write(stdin_text)
            except (BrokenPipeError, OSError):
                pass
            with proc.stdout:
                for line in proc.stdout:
                    events.put(line)
            code = proc.wait()
            if stopping and state is not None:
                try:
                    clean_stopped_downloads(state)
                except (OSError, ClipperError) as error:
                    events.put(f"Could not clean up the stopped job: {error}\n")
            events.put(code)

        threading.Thread(target=collect).start()

    def drain():
        nonlocal process
        while True:
            try:
                item = events.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, int):
                process = None
                status.set("Stopped" if stopping else "Completed" if item == 0 else "Needs attention — see activity")
                start.configure(state="normal")
                connect_button.configure(state="normal")
                stop.configure(state="disabled")
            else:
                log_line(item)
        root.after(120, drain)

    def run():
        try:
            chosen = int(count.get())
            if not 1 <= chosen <= 20:
                raise ValueError()
        except ValueError:
            messagebox.showerror("Clip count", "Choose a clip count between 1 and 20.", parent=root)
            return
        try:
            text = ""
            if following():
                value = channel.get().strip()
                if not (channel_handle(value) or channel_id(value)):
                    raise ClipperError("Enter a YouTube channel URL, @handle, or UC channel ID.")
                arguments = ["follow", value]
                if latest.get():
                    arguments.append("--include-latest")
            else:
                text = urls.get("1.0", "end").strip()
                parse_links(text)
                arguments = ["links"]
                if scope.get() == "Each video":
                    arguments.append("--per-video")
            arguments.extend(["--workflow", workflow.get(), "--clips", str(chosen),
                              "--caption-style", captions.get().lower(), "--output", str(output)])
            if visibility.get() != "Save locally":
                arguments.extend(["--publish", visibility.get().split()[-1]])
            spawn(arguments, text)
        except (ClipperError, OSError, ValueError) as error:
            messagebox.showerror("Shorts Studio", str(error), parent=root)

    def stop_job():
        nonlocal stopping
        if process is None or process.poll() is not None:
            return
        stopping = True
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        status.set("Stopping")

    def connect():
        path = filedialog.askopenfilename(title="Google Desktop OAuth client", filetypes=[("OAuth client JSON", "*.json")])
        if path:
            spawn(["connect", path, "--workflow", workflow.get()])

    def reveal():
        folder = workflow_output(output, workflow.get()) / "clips"
        if not folder.is_dir():
            messagebox.showinfo("Clips folder", f"Your first clips will appear in {folder}.", parent=root)
        elif os.name == "nt":
            os.startfile(folder)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])

    def close():
        if process is not None and process.poll() is None:
            if not messagebox.askyesno("Stop the worker?", "Closing Studio will stop the current job.", parent=root):
                return
            stop_job()
        root.destroy()

    start.configure(command=run)
    stop.configure(command=stop_job)
    connect_button.configure(command=connect)
    open_clips.configure(command=reveal)
    tabs.bind("<<NotebookTabChanged>>", mode_changed)
    def workflow_changed(*_):
        captions.set(default_caption(workflow.get()).capitalize())
        mode_changed()

    workflow.trace_add("write", workflow_changed)
    root.protocol("WM_DELETE_WINDOW", close)
    mode_changed()
    drain()
    root.mainloop()
    return 0
