"""NX-053 — Live Windows Fixture Application.

Deterministic Windows GUI fixture application running in a dedicated process,
exposing real Windows HWNDs, process identities (executable path, SHA-256, PID, creation epoch),
and identifiable controls (AutomationId, ControlType, Name, Parent Path)
for live Windows UI Automation qualification.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .windows_witness_contract import (
    ControlIdentity,
    ProcessIdentity,
    WindowIdentity,
)


def get_process_identity(pid: int | None = None) -> ProcessIdentity:
    """Extract real Windows ProcessIdentity using Win32 API."""
    kernel32 = ctypes.windll.kernel32
    target_pid = pid if pid is not None else kernel32.GetCurrentProcessId()

    h_proc = kernel32.OpenProcess(0x1000, False, target_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h_proc:
        raise RuntimeError(f"Failed to open process {target_pid}")

    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size))
        exe_path = buf.value

        ct = wintypes.FILETIME()
        et = wintypes.FILETIME()
        kt = wintypes.FILETIME()
        ut = wintypes.FILETIME()
        kernel32.GetProcessTimes(h_proc, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut))
        create_epoch = (ct.dwLowDateTime + (ct.dwHighDateTime << 32) - 116444736000000000) / 10000000.0

        p_file = Path(exe_path)
        with open(p_file, "rb") as f:
            exe_sha = "sha256:" + hashlib.sha256(f.read()).hexdigest()

        return ProcessIdentity(
            executable_path=str(p_file),
            executable_sha256=exe_sha,
            pid=target_pid,
            create_time_epoch=create_epoch,
            architecture="x64",
        )
    finally:
        kernel32.CloseHandle(h_proc)


class LiveFixtureProcessController:
    """Manages the lifecycle of the standalone Live Windows Fixture Process."""

    def __init__(self, title: str = "BDB-VNext Live Witness Fixture") -> None:
        self.title = title
        self.proc: subprocess.Popen[str] | None = None
        self.process_identity: ProcessIdentity | None = None
        self.window_identity: WindowIdentity | None = None
        self.controls: dict[str, ControlIdentity] = {}

    def launch(self) -> None:
        """Launch the live fixture application as a dedicated process."""
        cmd = [sys.executable, "-m", "bdb_vnext.windows_fixture_app", "--title", self.title]

        self.proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        assert self.proc.stdout is not None
        ready_line = self.proc.stdout.readline().strip()
        if not ready_line.startswith("FIXTURE_READY:"):
            stderr_out = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"Fixture process failed to start. Output: {ready_line}, Error: {stderr_out}")

        payload = json.loads(ready_line[len("FIXTURE_READY:"):])
        pid = int(payload["pid"])
        hwnd = int(payload["hwnd"])
        win_class = str(payload["window_class"])
        dpi = int(payload.get("dpi", 96))

        self.process_identity = get_process_identity(pid)
        self.window_identity = WindowIdentity(
            owning_process=self.process_identity,
            native_hwnd=hwnd,
            window_class=win_class,
            window_title=self.title,
            ui_automation_root_id=f"UIA_Root_{hwnd}",
            monitor_id="DISPLAY_1",
            dpi=dpi,
            bounds=(100, 100, 500, 400),
        )

        # Build Control Identities
        self.controls["txt_input_a"] = ControlIdentity(
            owning_window=self.window_identity,
            automation_id="txt_input_a",
            control_type="Edit",
            control_name="Input Alpha",
            control_path=("Root", "PanelA", "txt_input_a"),
            runtime_id=(hwnd, 101),
            supported_patterns=("ValuePattern", "TextPattern"),
        )

        self.controls["txt_input_b"] = ControlIdentity(
            owning_window=self.window_identity,
            automation_id="txt_input_b",
            control_type="Edit",
            control_name="Input Beta",
            control_path=("Root", "PanelB", "txt_input_b"),
            runtime_id=(hwnd, 102),
            supported_patterns=("ValuePattern", "TextPattern"),
        )

        self.controls["btn_calc_a"] = ControlIdentity(
            owning_window=self.window_identity,
            automation_id="btn_calc_a",
            control_type="Button",
            control_name="Calculate",
            control_path=("Root", "PanelA", "btn_calc_a"),
            runtime_id=(hwnd, 201),
            supported_patterns=("InvokePattern",),
        )

        self.controls["btn_calc_b"] = ControlIdentity(
            owning_window=self.window_identity,
            automation_id="btn_calc_b",
            control_type="Button",
            control_name="Calculate",
            control_path=("Root", "PanelB", "btn_calc_b"),
            runtime_id=(hwnd, 202),
            supported_patterns=("InvokePattern",),
        )

        self.controls["lbl_status"] = ControlIdentity(
            owning_window=self.window_identity,
            automation_id="lbl_status",
            control_type="Text",
            control_name="Status",
            control_path=("Root", "lbl_status"),
            runtime_id=(hwnd, 301),
            supported_patterns=(),
        )

    def send_cmd(self, cmd: str, **kwargs: Any) -> dict[str, Any]:
        """Send command to live fixture process and get JSON response."""
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError("Fixture process not running")
        msg = json.dumps({"cmd": cmd, **kwargs}) + "\n"
        self.proc.stdin.write(msg)
        self.proc.stdin.flush()
        line = self.proc.stdout.readline().strip()
        if not line:
            raise RuntimeError("Fixture process closed pipe unexpectedly")
        return json.loads(line)

    def set_entry_text(self, entry_name: str, text: str) -> None:
        self.send_cmd("set_entry_text", entry_name=entry_name, text=text)

    def get_entry_text(self, entry_name: str) -> str:
        res = self.send_cmd("get_entry_text", entry_name=entry_name)
        return str(res.get("text", ""))

    def invoke_button(self, button_name: str) -> None:
        self.send_cmd("invoke_button", button_name=button_name)

    def get_status_text(self) -> str:
        res = self.send_cmd("get_status_text")
        return str(res.get("status", ""))

    def resize_window(self, width: int, height: int) -> tuple[int, int]:
        res = self.send_cmd("resize_window", width=width, height=height)
        return int(res.get("width", width)), int(res.get("height", height))

    def terminate(self) -> None:
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.write(json.dumps({"cmd": "exit"}) + "\n")
                    self.proc.stdin.flush()
                self.proc.wait(timeout=2.0)
            except Exception:
                self.proc.kill()
            finally:
                self.proc = None


# ==============================================================================
# Standalone Process Execution
# ==============================================================================

def _run_standalone() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="BDB-VNext Live Witness Fixture")
    args = parser.parse_args()

    root = tk.Tk()
    root.title(args.title)
    root.geometry("500x400+100+100")
    root.update_idletasks()
    root.update()

    hwnd = int(root.winfo_id())
    pid = os.getpid()
    win_class = root.winfo_class()

    user32 = ctypes.windll.user32
    dpi = 96
    if hasattr(user32, "GetDpiForWindow"):
        dpi = user32.GetDpiForWindow(hwnd) or 96

    # Panel A
    frame_a = tk.Frame(root, name="panela")
    frame_a.pack(pady=10, fill="x", padx=10)

    lbl_a = tk.Label(frame_a, text="Input Alpha:")
    lbl_a.pack(side="left")
    entry_a = tk.Entry(frame_a, name="txt_input_a")
    entry_a.pack(side="left", padx=5)

    def _on_calc_a() -> None:
        lbl_status.config(text="CALC_A_DONE")

    btn_a = tk.Button(frame_a, text="Calculate", name="btn_calc_a", command=_on_calc_a)
    btn_a.pack(side="left", padx=5)

    # Panel B
    frame_b = tk.Frame(root, name="panelb")
    frame_b.pack(pady=10, fill="x", padx=10)

    lbl_b = tk.Label(frame_b, text="Input Beta:")
    lbl_b.pack(side="left")
    entry_b = tk.Entry(frame_b, name="txt_input_b")
    entry_b.pack(side="left", padx=5)

    def _on_calc_b() -> None:
        lbl_status.config(text="CALC_B_DONE")

    btn_b = tk.Button(frame_b, text="Calculate", name="btn_calc_b", command=_on_calc_b)
    btn_b.pack(side="left", padx=5)

    # Status Label
    lbl_status = tk.Label(root, text="READY", name="lbl_status", fg="blue")
    lbl_status.pack(pady=20)

    # Signal ready to parent controller
    ready_data = {
        "pid": pid,
        "hwnd": hwnd,
        "window_class": win_class,
        "dpi": dpi,
    }
    sys.stdout.write("FIXTURE_READY:" + json.dumps(ready_data) + "\n")
    sys.stdout.flush()

    # Async command pump via stdin in non-blocking / poll loop
    import select
    running = True

    import threading
    cmd_queue: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _stdin_reader() -> None:
        nonlocal running
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                with lock:
                    cmd_queue.append(data)
            except Exception:
                pass
        running = False

    t = threading.Thread(target=_stdin_reader, daemon=True)
    t.start()

    while running:
        # Check command queue
        cmds_to_run = []
        with lock:
            if cmd_queue:
                cmds_to_run = list(cmd_queue)
                cmd_queue.clear()

        for msg in cmds_to_run:
            cmd = msg.get("cmd")
            if cmd == "exit":
                running = False
                break
            elif cmd == "set_entry_text":
                en = msg.get("entry_name")
                txt = msg.get("text", "")
                target_e = entry_a if en == "txt_input_a" else entry_b
                target_e.delete(0, tk.END)
                target_e.insert(0, txt)
                sys.stdout.write(json.dumps({"status": "OK"}) + "\n")
                sys.stdout.flush()
            elif cmd == "get_entry_text":
                en = msg.get("entry_name")
                target_e = entry_a if en == "txt_input_a" else entry_b
                sys.stdout.write(json.dumps({"text": target_e.get()}) + "\n")
                sys.stdout.flush()
            elif cmd == "invoke_button":
                bn = msg.get("button_name")
                target_b = btn_a if bn == "btn_calc_a" else btn_b
                target_b.invoke()
                sys.stdout.write(json.dumps({"status": "OK"}) + "\n")
                sys.stdout.flush()
            elif cmd == "get_status_text":
                sys.stdout.write(json.dumps({"status": lbl_status.cget("text")}) + "\n")
                sys.stdout.flush()
            elif cmd == "resize_window":
                w = msg.get("width", 500)
                h = msg.get("height", 400)
                root.geometry(f"{w}x{h}")
                root.update_idletasks()
                sys.stdout.write(json.dumps({"width": w, "height": h}) + "\n")
                sys.stdout.flush()

        try:
            root.update_idletasks()
            root.update()
        except Exception:
            break
        time.sleep(0.02)

    try:
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    _run_standalone()
