"""Simple cross-platform Tkinter setup wizard.

The UI intentionally has no dependency on a browser or local HTTP listener,
so the same source can be packaged as a macOS app and a Windows executable.
"""

from __future__ import annotations

import datetime as _datetime
import platform
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk
from typing import Any
from pathlib import Path

from xiaobai_connector import __version__
from xiaobai_connector.config import ConnectorConfig, VALID_SANDBOXES
from xiaobai_connector.credentials import CredentialStore, SERVICE
from xiaobai_connector.discovery import AGENT_DISPLAY_NAMES, resolve_executable, scan_agents
from xiaobai_connector.pairing import PairingClient, PairingError, PairingPending, PairingRequest
from xiaobai_connector.paths import ensure_data_dir, log_path
from xiaobai_connector.runtime import ConnectorRuntime


COLORS = {
    "canvas": "#f4f4f4",
    "white": "#ffffff",
    "ink": "#111111",
    "muted": "#686868",
    "quiet": "#8a8a8a",
    "line": "#d6d6d6",
    "soft": "#ededed",
    "disabled": "#a7a7a7",
}


class ConnectorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("小白 Connector")
        self.geometry("760x640")
        self.minsize(680, 560)
        self.configure(bg=COLORS["canvas"])
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.config_value = ConnectorConfig.load()
        self.candidates: list[Any] = []
        self.check_vars: list[tk.BooleanVar] = []
        self.pairing: PairingRequest | None = None
        self.pairing_started_at = 0.0
        self.runtime: ConnectorRuntime | None = None
        self._polling = False
        self._scan_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.report_callback_exception = self._report_callback_exception
        self._build_style()
        self._build_header()
        self.body = ttk.Frame(self, style="App.TFrame", padding=(34, 16, 34, 14))
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        self.page = ttk.Frame(self.body, style="App.TFrame")
        self.page.grid(row=0, column=0, sticky="nsew")
        ttk.Separator(self, orient="horizontal").grid(
            row=2, column=0, sticky="ew")
        self._build_navigation()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.show_scan()
        self.after(150, self._auto_connect_if_configured)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=COLORS["canvas"])
        style.configure("App.TFrame", background=COLORS["canvas"])
        style.configure("Navigation.TFrame", background=COLORS["canvas"])
        style.configure("Card.TFrame", background=COLORS["white"],
                        relief="solid", borderwidth=1)
        style.configure("CardInner.TFrame", background=COLORS["white"])

        style.configure("TLabel", background=COLORS["canvas"], foreground=COLORS["ink"])
        style.configure("Title.TLabel", background=COLORS["canvas"],
                        font=("Arial", 24, "bold"), foreground=COLORS["ink"])
        style.configure("Heading.TLabel", background=COLORS["canvas"],
                        font=("Arial", 18, "bold"), foreground=COLORS["ink"])
        style.configure("Sub.TLabel", background=COLORS["canvas"],
                        font=("Arial", 11), foreground=COLORS["muted"])
        style.configure("Small.TLabel", background=COLORS["canvas"],
                        font=("Arial", 9), foreground=COLORS["quiet"])
        style.configure("Meta.TLabel", background=COLORS["canvas"],
                        font=("Arial", 9, "bold"), foreground=COLORS["quiet"])
        style.configure("CardLabel.TLabel", background=COLORS["white"],
                        foreground=COLORS["ink"])
        style.configure("CardMeta.TLabel", background=COLORS["white"],
                        font=("Arial", 9), foreground=COLORS["quiet"])
        style.configure("CardDetail.TLabel", background=COLORS["white"],
                        font=("Arial", 9), foreground=COLORS["muted"])
        style.configure("PairingId.TLabel", background=COLORS["white"],
                        font=("Courier", 18, "bold"), foreground=COLORS["ink"])
        style.configure("PairingCode.TLabel", background=COLORS["white"],
                        font=("Courier", 32, "bold"), foreground=COLORS["ink"])
        style.configure("Error.TLabel", background=COLORS["canvas"],
                        font=("Arial", 9), foreground=COLORS["ink"])

        style.configure("TButton", background=COLORS["white"], foreground=COLORS["ink"],
                        bordercolor=COLORS["line"], lightcolor=COLORS["white"],
                        darkcolor=COLORS["line"], relief="solid", borderwidth=1,
                        padding=(14, 8), font=("Arial", 10))
        style.map(
            "TButton",
            background=[("disabled", COLORS["soft"]), ("pressed", COLORS["soft"]),
                        ("active", COLORS["soft"])],
            foreground=[("disabled", COLORS["disabled"])],
            bordercolor=[("focus", COLORS["ink"])],
        )
        style.configure("Primary.TButton", background=COLORS["ink"],
                        foreground=COLORS["white"], bordercolor=COLORS["ink"],
                        lightcolor=COLORS["ink"], darkcolor=COLORS["ink"],
                        relief="solid", borderwidth=1, padding=(18, 9),
                        font=("Arial", 10, "bold"))
        style.map(
            "Primary.TButton",
            background=[("disabled", COLORS["disabled"]), ("pressed", "#000000"),
                        ("active", "#303030")],
            foreground=[("disabled", COLORS["white"])],
            bordercolor=[("focus", COLORS["ink"])],
        )

        style.configure("TCheckbutton", background=COLORS["canvas"],
                        foreground=COLORS["ink"], font=("Arial", 10))
        style.configure("Card.TCheckbutton", background=COLORS["white"],
                        foreground=COLORS["ink"], font=("Arial", 11))
        style.map("Card.TCheckbutton", background=[("active", COLORS["white"]),
                                                     ("disabled", COLORS["white"])],
                  foreground=[("disabled", COLORS["disabled"])])

        style.configure("TEntry", fieldbackground=COLORS["white"],
                        foreground=COLORS["ink"], bordercolor=COLORS["line"],
                        lightcolor=COLORS["line"], darkcolor=COLORS["line"],
                        padding=(8, 7))
        style.map("TEntry", bordercolor=[("focus", COLORS["ink"])],
                  lightcolor=[("focus", COLORS["ink"])],
                  darkcolor=[("focus", COLORS["ink"])])
        style.configure("TCombobox", fieldbackground=COLORS["white"],
                        background=COLORS["white"], foreground=COLORS["ink"],
                        bordercolor=COLORS["line"], lightcolor=COLORS["line"],
                        darkcolor=COLORS["line"], padding=(8, 6))
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["white"])],
                  foreground=[("readonly", COLORS["ink"])],
                  bordercolor=[("focus", COLORS["ink"])])

    def _build_header(self) -> None:
        header = ttk.Frame(self, style="App.TFrame", padding=(34, 24, 34, 10))
        header.grid(row=0, column=0, sticky="ew")
        title_row = ttk.Frame(header, style="App.TFrame")
        title_row.pack(fill="x")
        ttk.Label(title_row, text="小白 Connector", style="Title.TLabel").pack(side="left")
        ttk.Label(title_row, text=f"v{__version__}", style="Meta.TLabel").pack(
            side="right", pady=(10, 0))
        self.header_subtitle = ttk.Label(
            header, text="把这台电脑里的 Agent 安全连接到手机", style="Sub.TLabel")
        self.header_subtitle.pack(anchor="w", pady=(4, 0))

    def _build_navigation(self) -> None:
        navigation = ttk.Frame(self, style="Navigation.TFrame", padding=(34, 12, 34, 18))
        navigation.grid(row=3, column=0, sticky="ew")
        navigation.columnconfigure(0, weight=1)
        self.nav_back = ttk.Button(navigation, text="")
        self.nav_back.grid(row=0, column=0, sticky="w")
        self.nav_next = ttk.Button(navigation, text="", style="Primary.TButton")
        self.nav_next.grid(row=0, column=1, sticky="e")
        self.nav_back.grid_remove()
        self.nav_next.grid_remove()

    def _clear(self) -> None:
        for child in self.page.winfo_children():
            child.destroy()

    def _heading(self, title: str, subtitle: str) -> None:
        ttk.Label(self.page, text=title, style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.page, text=subtitle, style="Sub.TLabel", wraplength=660).pack(
            anchor="w", pady=(5, 18))

    def show_scan(self) -> None:
        self._clear()
        self._bottom_buttons(None, None, next_text="")
        self._heading(
            "1. 选择要连接的 Agent",
            "程序会检查常见的本地安装位置；如果没有找到，可以手动选择 Agent 的 .exe、.cmd 或可执行文件。",
        )
        top = ttk.Frame(self.page, style="App.TFrame")
        top.pack(fill="x")
        self.scan_status = ttk.Label(top, text="正在扫描…", style="Small.TLabel")
        self.scan_status.pack(side="left")
        ttk.Button(top, text="手动添加路径…", command=self._choose_agent_path).pack(
            side="right", padx=(8, 0))
        ttk.Button(top, text="重新扫描", command=self._scan_async).pack(side="right")
        self.agent_frame = ttk.Frame(self.page, style="App.TFrame")
        self.agent_frame.pack(fill="both", expand=True, pady=(12, 8))
        self.scan_status_value = "正在扫描…"
        self._scan_async()

    def _scan_async(self) -> None:
        self.scan_status.configure(text="正在扫描本机 Agent…")
        self._bottom_buttons(None, None, next_text="")
        for child in self.agent_frame.winfo_children():
            child.destroy()
        while True:
            try:
                self._scan_queue.get_nowait()
            except queue.Empty:
                break
        threading.Thread(target=self._scan_worker, name="agent-discovery", daemon=True).start()
        self.after(80, self._poll_scan_result)

    def _scan_worker(self) -> None:
        try:
            result = scan_agents(self.config_value.agents)
            self._scan_queue.put(("ok", result))
        except Exception as exc:
            self._scan_queue.put(("error", str(exc)))

    def _poll_scan_result(self) -> None:
        try:
            state, value = self._scan_queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_scan_result)
            return
        if state == "ok":
            self._show_candidates(value)
        else:
            self.scan_status.configure(text=f"扫描失败：{value}")

    def _show_candidates(self, candidates: list[Any]) -> None:
        self.candidates = candidates
        self.check_vars = []
        for candidate in candidates:
            card = ttk.Frame(self.agent_frame, style="Card.TFrame", padding=14)
            card.pack(fill="x", pady=5)
            row = ttk.Frame(card, style="CardInner.TFrame")
            row.pack(fill="x")
            variable = tk.BooleanVar(value=bool(candidate.selected and candidate.can_connect))
            self.check_vars.append(variable)
            checkbox = ttk.Checkbutton(
                row, text=candidate.display_name, variable=variable,
                style="Card.TCheckbutton",
                state="normal" if candidate.can_connect else "disabled")
            checkbox.pack(side="left")
            state = "可连接" if candidate.status == "online" else "已配置" if candidate.status == "configured" else "未找到"
            actions = ttk.Frame(row, style="CardInner.TFrame")
            actions.pack(side="right")
            ttk.Label(actions, text=state, style="CardMeta.TLabel").pack(side="left")
            ttk.Button(
                actions,
                text="更换路径…" if candidate.can_connect else "选择路径…",
                command=lambda kind=candidate.kind: self._choose_agent_path(kind),
            ).pack(side="left", padx=(10, 0))
            detail = candidate.detail
            if candidate.version:
                detail += f" · {candidate.version}"
            if candidate.executable:
                detail += f"\n{candidate.executable}"
            ttk.Label(card, text=detail, style="CardDetail.TLabel", wraplength=610).pack(
                anchor="w", pady=(7, 0))
        found = sum(1 for item in candidates if item.found)
        self.scan_status.configure(text=f"扫描完成：发现 {found} 个 Agent")
        self._bottom_buttons(None, self._to_scope, next_text="下一步")

    def _selected_candidates(self) -> list[Any]:
        return [candidate for candidate, variable in zip(self.candidates, self.check_vars)
                if variable.get() and candidate.can_connect]

    def _choose_agent_path(self, kind: str | None = None) -> None:
        """Open a small form for selecting or pasting a local Agent path."""
        dialog = tk.Toplevel(self)
        dialog.title("添加 Agent 路径")
        dialog.transient(self)
        dialog.configure(bg=COLORS["canvas"])
        dialog.geometry("640x250")
        dialog.minsize(600, 250)
        dialog.resizable(True, False)
        dialog.grab_set()

        content = ttk.Frame(dialog, style="App.TFrame", padding=22)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="Agent 类型").grid(row=0, column=0, sticky="w", pady=6)

        kind_values = [AGENT_DISPLAY_NAMES[item] for item in AGENT_DISPLAY_NAMES]
        initial_kind = kind if kind in AGENT_DISPLAY_NAMES else "codex"
        kind_var = tk.StringVar(value=AGENT_DISPLAY_NAMES[initial_kind])
        kind_combo = ttk.Combobox(
            content, textvariable=kind_var, values=kind_values, state="readonly", width=28)
        kind_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(14, 0), pady=6)

        ttk.Label(content, text="程序路径").grid(row=1, column=0, sticky="w", pady=6)
        path_var = tk.StringVar()
        path_entry = ttk.Entry(content, textvariable=path_var, width=48)
        path_entry.grid(row=1, column=1, sticky="ew", padx=(14, 8), pady=6)

        def browse() -> None:
            value = filedialog.askopenfilename(
                parent=dialog,
                title="选择 Agent 程序",
                filetypes=[
                    ("Agent 程序", "*.exe *.cmd *.bat *.com"),
                    ("所有文件", "*.*"),
                ],
            )
            if value:
                path_var.set(value)

        ttk.Button(content, text="浏览…", command=browse).grid(
            row=1, column=2, sticky="e", pady=6)
        ttk.Label(
            content,
            text="Windows 可选择 .exe、.cmd 或 .bat；也可以直接粘贴完整路径。",
            style="Small.TLabel",
            wraplength=460,
        ).grid(row=2, column=1, columnspan=2, sticky="w", padx=(14, 0), pady=(0, 8))
        error = ttk.Label(content, text="", style="Error.TLabel", wraplength=460)
        error.grid(row=3, column=1, columnspan=2, sticky="w", padx=(14, 0), pady=(0, 4))

        buttons = ttk.Frame(content, style="App.TFrame")
        buttons.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left")

        def add_path() -> None:
            selected_kind = next(
                (item for item, label in AGENT_DISPLAY_NAMES.items() if label == kind_var.get()),
                "",
            )
            executable = resolve_executable(path_var.get())
            if not selected_kind:
                error.configure(text="请选择 Agent 类型。")
                return
            if not executable:
                error.configure(text="路径不存在或不可执行，请选择 Agent 的实际程序文件。")
                return
            self._remember_agent_path(selected_kind, executable)
            dialog.destroy()
            self._scan_async()

        ttk.Button(buttons, text="添加并重新扫描", style="Primary.TButton",
                   command=add_path).pack(side="right")
        content.columnconfigure(1, weight=1)
        path_entry.focus_set()
        self.wait_window(dialog)

    def _remember_agent_path(self, kind: str, executable: str) -> None:
        definitions = [dict(item) for item in self.config_value.agents]
        updated = False
        for definition in definitions:
            if str(definition.get("adapter") or "").strip().lower() != kind:
                continue
            definition.update({
                "adapter": kind,
                "display_name": AGENT_DISPLAY_NAMES[kind],
                "mention_handle": kind,
                "binary": executable,
                "local_ref": str(definition.get("local_ref") or f"{kind}:default"),
                "enabled": False,
            })
            updated = True
            break
        if not updated:
            definitions.append({
                "local_ref": f"{kind}:default",
                "adapter": kind,
                "display_name": AGENT_DISPLAY_NAMES[kind],
                "mention_handle": kind,
                "binary": executable,
                "enabled": False,
            })
        self.config_value.agents = definitions
        try:
            self.config_value.save()
        except (OSError, ValueError) as exc:
            self.scan_status.configure(text=f"路径已添加，但保存失败：{exc}")

    def _to_scope(self) -> None:
        if not self._selected_candidates():
            messagebox.showinfo("还没有选择", "请至少勾选一个可以连接的 Agent。", parent=self)
            return
        self.show_scope()

    def show_scope(self) -> None:
        self._clear()
        self._heading("2. 设置这台电脑", "这些设置会保存在本机；配对完成后可以再次修改。")
        form = ttk.Frame(self.page, style="App.TFrame")
        form.pack(fill="x")
        ttk.Label(form, text="电脑名称").grid(row=0, column=0, sticky="w", pady=8)
        self.device_name_var = tk.StringVar(value=self.config_value.device_name or platform.node())
        ttk.Entry(form, textvariable=self.device_name_var, width=48).grid(
            row=0, column=1, sticky="ew", padx=(18, 0), pady=8)
        ttk.Label(form, text="手机上会显示这个名称", style="Small.TLabel").grid(
            row=1, column=1, sticky="w", padx=(18, 0))
        ttk.Label(form, text="工作目录").grid(row=2, column=0, sticky="w", pady=8)
        self.workdir_var = tk.StringVar(value=self.config_value.workdir or str(Path.home()))
        work_row = ttk.Frame(form, style="App.TFrame")
        work_row.grid(row=2, column=1, sticky="ew", padx=(18, 0), pady=8)
        ttk.Entry(work_row, textvariable=self.workdir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(work_row, text="选择…", command=self._choose_workdir).pack(side="left", padx=(8, 0))
        ttk.Label(form, text="安全策略").grid(row=3, column=0, sticky="w", pady=8)
        self.sandbox_var = tk.StringVar(value=self.config_value.sandbox or "workspace-write")
        sandbox = ttk.Combobox(form, textvariable=self.sandbox_var, values=VALID_SANDBOXES,
                               state="readonly", width=30)
        sandbox.grid(row=3, column=1, sticky="w", padx=(18, 0), pady=8)
        self.warning = ttk.Label(form, text="默认允许 Agent 修改所选工作目录，不允许越过目录操作。",
                                 style="Small.TLabel", wraplength=500)
        self.warning.grid(row=4, column=1, sticky="w", padx=(18, 0), pady=(0, 18))
        sandbox.bind("<<ComboboxSelected>>", self._sandbox_changed)
        form.columnconfigure(1, weight=1)
        self._bottom_buttons(self.show_scan, self._to_pairing, next_text="生成配对码")

    def _sandbox_changed(self, _event: Any = None) -> None:
        value = self.sandbox_var.get()
        if value == "danger-full-access":
            self.warning.configure(text="高风险：Agent 可以读写本机全部文件。只有明确需要时才选择。",
                                   foreground=COLORS["ink"])
        elif value == "read-only":
            self.warning.configure(text="只读：适合查看和问答，Agent 不能修改文件。",
                                   foreground=COLORS["muted"])
        else:
            self.warning.configure(text="默认允许 Agent 修改所选工作目录，不允许越过目录操作。",
                                   foreground=COLORS["muted"])

    def _choose_workdir(self) -> None:
        value = filedialog.askdirectory(parent=self, title="选择 Agent 工作目录")
        if value:
            self.workdir_var.set(value)

    def _to_pairing(self) -> None:
        name = self.device_name_var.get().strip()
        workdir = self.workdir_var.get().strip()
        if not name:
            messagebox.showerror("信息不完整", "请输入电脑名称。", parent=self)
            return
        if not workdir:
            messagebox.showerror("信息不完整", "请选择工作目录。", parent=self)
            return
        self.config_value.device_name = name
        self.config_value.workdir = workdir
        self.config_value.sandbox = self.sandbox_var.get()
        self.config_value.agents = [candidate.as_config(workdir, self.sandbox_var.get())
                                    for candidate in self._selected_candidates()]
        self.show_pairing()

    def show_pairing(self) -> None:
        self._clear()
        self._heading("3. 用手机完成配对", "打开小白 App → 设置 → 添加电脑，输入下面的 pairing ID 和 6 位数字。")
        card = ttk.Frame(self.page, style="Card.TFrame", padding=24)
        card.pack(fill="x", pady=(4, 16))
        self.pairing_id_var = tk.StringVar(value="正在生成…")
        self.short_code_var = tk.StringVar(value="------")
        ttk.Label(card, text="pairing ID", style="CardMeta.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=self.pairing_id_var, style="PairingId.TLabel").pack(
            anchor="w", pady=(3, 16))
        ttk.Label(card, text="6 位配对码", style="CardMeta.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=self.short_code_var, style="PairingCode.TLabel").pack(
            anchor="w", pady=(3, 8))
        copy_row = ttk.Frame(card, style="CardInner.TFrame")
        copy_row.pack(anchor="w", pady=(3, 0))
        ttk.Button(copy_row, text="复制 pairing ID", command=lambda: self._copy(self.pairing_id_var.get())).pack(side="left")
        ttk.Button(copy_row, text="复制配对码", command=lambda: self._copy(self.short_code_var.get())).pack(side="left", padx=(8, 0))
        self.pairing_status = ttk.Label(self.page, text="正在连接配对服务…", style="Sub.TLabel", wraplength=650)
        self.pairing_status.pack(anchor="w")
        self.expiry_label = ttk.Label(self.page, text="", style="Small.TLabel")
        self.expiry_label.pack(anchor="w", pady=(6, 0))
        self._polling = True
        self._bottom_buttons(self._cancel_pairing, None, next_text="", back_text="取消")
        threading.Thread(target=self._pairing_worker, name="pairing", daemon=True).start()

    def _pairing_worker(self) -> None:
        try:
            client = PairingClient(self.config_value.server_url)
            pairing = client.start(device_name=self.config_value.device_name,
                                   platform=self.config_value.platform,
                                   connector_version=__version__)
            self.pairing = pairing
            self.pairing_started_at = time.time()
            self.after(0, lambda: self._show_pairing(pairing))
            while self._polling is not False:
                try:
                    result = client.exchange(pairing.pairing_id, pairing.proof_secret)
                except PairingPending:
                    time.sleep(2)
                    continue
                self.after(0, lambda result=result: self._pairing_success(result))
                return
        except (PairingError, OSError) as exc:
            error = str(exc)
            self.after(0, lambda error=error: self._show_pairing_error(error))

    def _show_pairing_error(self, error: str) -> None:
        if not hasattr(self, "pairing_status") or not self.pairing_status.winfo_exists():
            return
        self.pairing_status.configure(text=f"配对失败：{error}", foreground=COLORS["ink"])

    def _show_pairing(self, pairing: PairingRequest) -> None:
        if not hasattr(self, "pairing_id_var") or not self.pairing_id_var.winfo_exists():
            return
        self._polling = True
        self.pairing_id_var.set(pairing.pairing_id)
        self.short_code_var.set(pairing.short_code)
        self.pairing_status.configure(text="等待手机确认…", foreground=COLORS["muted"])
        self._update_expiry()

    def _update_expiry(self) -> None:
        if not self.pairing or not self._polling:
            return
        remaining = max(0, 300 - int(time.time() - self.pairing_started_at))
        self.expiry_label.configure(text=f"配对码约 {remaining // 60}:{remaining % 60:02d} 后过期")
        if remaining <= 0:
            self._polling = False
            self.pairing_status.configure(text="配对码已过期，请返回后重新生成。",
                                           foreground=COLORS["ink"])
            return
        self.after(1000, self._update_expiry)

    def _pairing_success(self, result: dict[str, str]) -> None:
        if not self._polling:
            return
        self._polling = False
        try:
            self.config_value.device_id = result["device_id"]
            self.config_value.connector_version = __version__
            self.config_value.save()
            CredentialStore(SERVICE).set(result["device_id"], result["connector_token"])
        except Exception as exc:
            self.pairing_status.configure(text=f"凭据保存失败：{exc}", foreground=COLORS["ink"])
            return
        self.pairing_status.configure(text="手机已确认，正在启动连接…", foreground=COLORS["ink"])
        self.show_connected()
        self._start_runtime()

    def show_connected(self) -> None:
        self._clear()
        self._heading("已连接这台电脑", "手机现在可以看到并使用下列 Agent。程序可以最小化到后台运行。")
        card = ttk.Frame(self.page, style="Card.TFrame", padding=18)
        card.pack(fill="x", pady=(3, 14))
        ttk.Label(card, text=self.config_value.device_name, style="CardLabel.TLabel",
                  font=("Arial", 15, "bold")).pack(anchor="w")
        ttk.Label(card, text=self.config_value.device_id, style="CardMeta.TLabel").pack(
            anchor="w", pady=(4, 12))
        for item in self.config_value.selected_agents:
            ttk.Label(card, text=f"✓  {item.get('display_name') or item.get('adapter')}",
                      style="CardLabel.TLabel", font=("Arial", 11)).pack(anchor="w", pady=3)
        self.connection_status = ttk.Label(self.page, text="正在启动…", style="Sub.TLabel")
        self.connection_status.pack(anchor="w", pady=(5, 18))
        self._bottom_buttons(self._re_pair, self._stop_runtime,
                             next_text="停止连接", back_text="重新配对")

    def _start_runtime(self) -> None:
        if self.runtime:
            self.runtime.stop()
        self.runtime = ConnectorRuntime(self.config_value, on_status=self._runtime_status)
        self.runtime.start()

    def _runtime_status(self, state: str, detail: str) -> None:
        self.after(0, lambda: self._apply_runtime_status(state, detail))

    def _apply_runtime_status(self, state: str, detail: str) -> None:
        if hasattr(self, "connection_status") and self.connection_status.winfo_exists():
            labels = {"online": "在线", "connecting": "连接中", "offline": "离线重试中",
                      "stopped": "已停止", "revoked": "凭据已撤销", "error": "错误"}
            foreground = (COLORS["ink"] if state in {"online", "stopped"}
                          else COLORS["muted"] if state in {"connecting", "offline"}
                          else COLORS["ink"])
            self.connection_status.configure(text=f"状态：{labels.get(state, state)}"
                                             + (f" · {detail}" if detail else ""),
                                             foreground=foreground)

    def _auto_connect_if_configured(self) -> None:
        if not self.config_value.device_id or not self.config_value.selected_agents:
            return
        if not CredentialStore(SERVICE).get(self.config_value.device_id):
            return
        self.show_connected()
        self._start_runtime()

    def _re_pair(self) -> None:
        self._stop_runtime()
        self.show_scan()

    def _stop_runtime(self) -> None:
        if self.runtime:
            self.runtime.stop()
            self.runtime = None
        self._apply_runtime_status("stopped", "连接已停止")

    def _cancel_pairing(self) -> None:
        self._polling = False
        self.show_scope()

    def _copy(self, value: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update()

    def _bottom_buttons(self, back: Any, next_action: Any, *,
                        next_text: str, back_text: str = "上一步") -> None:
        if back is None:
            self.nav_back.grid_remove()
        else:
            self.nav_back.configure(text=back_text, command=back)
            self.nav_back.grid()
        if next_action is None:
            self.nav_next.grid_remove()
        else:
            self.nav_next.configure(text=next_text, command=next_action)
            self.nav_next.grid()

    def _close(self) -> None:
        self._polling = False
        if self.runtime:
            self.runtime.stop()
        self.destroy()

    def _report_callback_exception(self, exc_type: Any, exc_value: Any,
                                   exc_traceback: Any) -> None:
        try:
            ensure_data_dir()
            with log_path().open("a", encoding="utf-8") as handle:
                traceback.print_exception(exc_type, exc_value, exc_traceback, file=handle)
        except OSError:
            pass


def main() -> None:
    # PyInstaller uses the same executable for the optional Claude MCP child.
    # A frozen executable cannot execute a bundled .py path as a script, so
    # reserve a small headless entry point for that child process.
    if "--message-agent-mcp" in sys.argv:
        from xiaobai_connector.message_agent_mcp import main as mcp_main
        raise SystemExit(mcp_main())
    app = ConnectorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
