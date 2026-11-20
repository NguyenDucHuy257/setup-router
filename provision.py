"""AutoDoor router provisioner (Tkinter). Run:  python provision.py

Step 1  fill the connection + Tailscale fields, press Enter  -> the tool reads the router.
Step 2  adjust the proposed values (device id, LAN CIDR, route, ...), press Enter -> confirm -> it installs.
Step 3  the JSON is saved in ./output/<device_id>.json for the web's "Quản lý router" import.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import core

OUTPUT = Path(__file__).resolve().parent / "output"


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=12)
        root.title("AutoDoor - Cài đặt router OpenWrt")
        root.geometry("900x760")
        self.pack(fill="both", expand=True)
        self.phase = "input"          # input -> review -> done
        self.router: core.Router | None = None
        self.info: dict = {}
        self.messages: queue.Queue = queue.Queue()
        self.vars: dict[str, tk.Variable] = {}
        self.review_widgets: list[tk.Widget] = []
        self._build()
        root.bind("<Return>", self._enter)
        self.after(100, self._drain)

    # ---- layout -------------------------------------------------------------------------------

    def _field(self, parent, row: int, key: str, label: str, default="", secret=False, hint="") -> ttk.Entry:
        var = tk.StringVar(value=default)
        self.vars[key] = var
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
        entry = ttk.Entry(parent, textvariable=var, width=38, show="*" if secret else "")
        entry.grid(row=row, column=1, sticky="we", pady=2)
        if hint:
            ttk.Label(parent, text=hint, foreground="#666").grid(row=row, column=2, sticky="w", padx=8)
        return entry

    def _build(self) -> None:
        a = ttk.LabelFrame(self, text="1. Thông tin nhập", padding=8)
        a.pack(fill="x")
        a.columnconfigure(1, weight=1)
        self._field(a, 0, "host", "IP OpenWrt", "192.168.1.1")
        self._field(a, 1, "user", "Tài khoản SSH", "root")
        self._field(a, 2, "password", "Mật khẩu hiện tại", secret=True, hint="để trống nếu router mới/chưa đặt")
        self._field(a, 3, "new_password", "Mật khẩu mới", secret=True, hint="để trống = giữ nguyên (≥ 8 ký tự)")
        self._field(a, 4, "auth_key", "Tailscale auth key", secret=True, hint="tskey-auth-... (không lưu lại)")
        self._field(a, 5, "site_id", "Site ID (via)", hint="số web cấp, 1-65535, không được trùng")
        self._field(a, 6, "name", "Tên thiết bị", hint="ví dụ: Trọ Nguyễn Văn A - Cổng chính")

        b = ttk.LabelFrame(self, text="2. Thông số điều chỉnh (hiện sau khi kiểm tra)", padding=8)
        b.pack(fill="x", pady=(8, 0))
        b.columnconfigure(1, weight=1)
        for row, (key, label, default, hint) in enumerate([
            ("device_id", "Mã thiết bị", "", "8 ký tự ngẫu nhiên, sửa được"),
            ("cidr", "Mạng cần advertise", "", "LAN của router, hoặc mạng cha (WAN) nơi có camera"),
            ("tag", "Tag", "tag:site-router", "phải khai trong ACL"),
            ("heartbeat", "URL heartbeat (HTTPS)", "", "để trống nếu web chưa có API"),
            ("ssh_key", "SSH public key server", "", "tuỳ chọn, để đăng nhập không cần mật khẩu"),
            ("tg_token", "Telegram bot token", "", "tuỳ chọn: router tự báo khi phải restart Tailscale"),
            ("tg_chat", "Telegram chat ID nhóm", "", "tuỳ chọn, ví dụ -1001234567890"),
        ]):
            self.review_widgets.append(self._field(b, row, key, label, default, hint=hint))
        ttk.Label(b, text="Route 4via6").grid(row=7, column=0, sticky="w", pady=2)
        self.route_label = ttk.Label(b, text="-", font=("Consolas", 10), foreground="#0a5")
        self.route_label.grid(row=7, column=1, columnspan=2, sticky="w")
        self.relogin = tk.BooleanVar(value=True)
        r = ttk.Checkbutton(b, text="Đăng xuất Tailscale cũ trước (cần khi đổi sang tài khoản/tailnet khác)",
                            variable=self.relogin)
        r.grid(row=9, column=1, columnspan=2, sticky="w")
        self.review_widgets.append(r)
        self.skip_space = tk.BooleanVar(value=False)
        c = ttk.Checkbutton(b, text="Bỏ qua kiểm tra dung lượng flash", variable=self.skip_space)
        c.grid(row=8, column=1, sticky="w")
        self.review_widgets.append(c)
        for key in ("site_id", "cidr"):
            self.vars[key].trace_add("write", lambda *_: self._preview())

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=8)
        self.primary = ttk.Button(bar, text="Kiểm tra router (Enter)", command=self._primary)
        self.primary.pack(side="left")
        self.reset_btn = ttk.Button(bar, text="Làm lại", command=self._reset)
        self.reset_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(bar, text="Mở thư mục kết quả", command=lambda: os.startfile(OUTPUT)
                                   if OUTPUT.exists() else None)
        self.open_btn.pack(side="right")

        self.log_box = tk.Text(self, height=14, state="disabled", wrap="word", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)
        self._set_review(False)

    def _set_review(self, enabled: bool) -> None:
        for widget in self.review_widgets:
            widget.configure(state="normal" if enabled else "disabled")

    # ---- helpers ------------------------------------------------------------------------------

    def log(self, text: str) -> None:
        self.messages.put(("log", text))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def ui(self, fn) -> None:
        self.messages.put(("call", fn))

    def val(self, key: str) -> str:
        return str(self.vars[key].get()).strip()

    def _busy(self, busy: bool) -> None:
        self.primary.configure(state="disabled" if busy else "normal")

    def _preview(self) -> None:
        try:
            self.route_label.configure(text=core.via6_route(int(self.val("site_id")), self.val("cidr")),
                                       foreground="#0a5")
        except (ValueError, TypeError):
            self.route_label.configure(text="(Site ID hoặc CIDR chưa hợp lệ)", foreground="#c00")

    def _reset(self) -> None:
        if self.router:
            self.router.close()
            self.router = None
        self.phase = "input"
        self._set_review(False)
        self.primary.configure(text="Kiểm tra router (Enter)", state="normal")
        self.log("— Làm lại từ đầu —")

    def _enter(self, _event=None) -> None:
        if str(self.primary["state"]) == "normal":
            self._primary()

    def _primary(self) -> None:
        {"input": self._do_probe, "review": self._do_apply, "done": self._reset}[self.phase]()

    def _worker(self, fn) -> None:
        self._busy(True)

        def run():
            try:
                fn()
            except core.ProvisionError as exc:
                self.log(f"✗ {exc}")
                self.ui(lambda: self._busy(False))
            except Exception as exc:  # noqa: BLE001 - show anything unexpected instead of dying silently
                self.log(f"✗ Lỗi không mong đợi: {exc!r}")
                self.ui(lambda: self._busy(False))

        threading.Thread(target=run, daemon=True).start()

    # ---- step 1: probe ------------------------------------------------------------------------

    def _do_probe(self) -> None:
        missing = [label for key, label in (("host", "IP OpenWrt"), ("auth_key", "Auth key"),
                                            ("site_id", "Site ID"), ("name", "Tên thiết bị")) if not self.val(key)]
        if missing:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập: " + ", ".join(missing))
            return
        if not self.val("site_id").isdigit():
            messagebox.showwarning("Site ID", "Site ID phải là số nguyên.")
            return
        host, user, pw = self.val("host"), self.val("user") or "root", str(self.vars["password"].get())

        def job():
            self.log(f"Kết nối SSH tới {user}@{host} ...")
            router = core.Router(host, user, pw)
            router.connect()
            self.router = router
            info = core.probe(router, self.log)
            self.info = info

            def show():
                self.vars["device_id"].set(self.vars["device_id"].get() or core.gen_device_id())
                self.vars["cidr"].set(info["lanCidr"])
                self._set_review(True)
                self._preview()
                self.phase = "review"
                self.primary.configure(text="Nạp cấu hình (Enter)")
                self._busy(False)
                self.log("✔ Kiểm tra xong. Xem/sửa mục 2 rồi nhấn Enter để nạp.")
                if info["clockSkewSeconds"] and abs(info["clockSkewSeconds"]) > 300:
                    self.log("  Lưu ý: đồng hồ router lệch, tool sẽ chỉnh theo giờ máy tính.")

            self.ui(show)

        self._worker(job)

    # ---- step 2: apply ------------------------------------------------------------------------

    def _params(self) -> core.Params:
        return core.Params(
            host=self.val("host"), ssh_user=self.val("user") or "root",
            ssh_password=str(self.vars["password"].get()), new_password=str(self.vars["new_password"].get()),
            auth_key=self.val("auth_key"), site_id=int(self.val("site_id")), device_name=self.val("name"),
            device_id=self.val("device_id").upper(), cidr=self.val("cidr"), tag=self.val("tag"),
            heartbeat_url=self.val("heartbeat"), ssh_public_key=self.val("ssh_key"),
            telegram_token=str(self.vars["tg_token"].get()).strip(), telegram_chat_id=self.val("tg_chat"),
            skip_space_check=self.skip_space.get(), relogin=self.relogin.get())

    def _do_apply(self) -> None:
        try:
            params = self._params()
            route = params.route
        except ValueError as exc:
            messagebox.showwarning("Thông số chưa hợp lệ", str(exc))
            return
        if len(params.device_id) < 4:
            messagebox.showwarning("Mã thiết bị", "Mã thiết bị quá ngắn.")
            return
        problems = core.validate(params, self.info)
        if problems:
            messagebox.showerror("Cần sửa trước khi nạp", "\n• ".join(["Các vấn đề:"] + problems))
            return
        if params.cidr not in (self.info["lanCidr"], self.info.get("wanCidr")):
            if not messagebox.askyesno(
                    "CIDR khác LAN thật của router",
                    f"Router đang dùng LAN {self.info['lanCidr']} nhưng bạn sắp advertise {params.cidr}.\n\n"
                    "Camera/thiết bị trong LAN thật sẽ KHÔNG truy cập được qua route này. "
                    "Chỉ tiếp tục nếu bạn cố ý.\n\nVẫn tiếp tục?"):
                return
        if params.heartbeat_url and not params.heartbeat_url.startswith("https://"):
            if not messagebox.askyesno("URL không phải HTTPS", "Heartbeat mang token nhưng URL không phải https://. "
                                       "Vẫn tiếp tục?"):
                return
        summary = (f"Router: {params.host}  ({self.info['openwrt']})\n"
                   f"Mã thiết bị: {params.device_id}   Tên: {params.device_name}\n"
                   f"Site ID: {params.site_id}\nLAN: {params.cidr}\nRoute 4via6:\n  {route}\nTag: {params.tag}\n"
                   f"Heartbeat: {params.heartbeat_url or '(chưa bật)'}\n"
                   f"Đổi mật khẩu: {'CÓ' if params.new_password else 'không'}\n\n"
                   "Tool sẽ cài Tailscale, sửa cấu hình mạng/tường lửa, đăng nhập tailnet. Tiếp tục?")
        if not messagebox.askyesno("Xác nhận nạp cấu hình", summary):
            return

        def job():
            assert self.router is not None
            facts = core.apply(self.router, params, self.info, self.log)
            if params.new_password:
                self.log("Kiểm tra đăng nhập lại bằng mật khẩu mới...")
                check = core.Router(params.host, params.ssh_user, params.new_password)
                check.connect()
                check.close()
                self.log("  Đăng nhập bằng mật khẩu mới OK")
            data = core.build_json(params, self.info, facts)
            OUTPUT.mkdir(exist_ok=True)
            path = OUTPUT / f"{params.device_id}.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            new_tailnet = data["tailscale"].get("tailnet") or "?"
            self.log(f"  Tailnet đang dùng: {new_tailnet}")
            old_tailnet = self.info.get("tsTailnet")
            if old_tailnet and old_tailnet == new_tailnet and params.relogin:
                self.log(f"⚠ Vẫn là tailnet cũ ({old_tailnet}). Auth key có thật sự thuộc tài khoản mới không?")
            self.log(f"✔ XONG. Đã lưu: {path}")
            self.log(f"  IP public: {data['network']['publicIp'] or '(không đọc được)'} | "
                     f"Tailscale IP: {', '.join(data['tailscale']['ips'])}")
            self.log("  Lên admin Tailscale kiểm tra: máy có tag, 'Key expiry disabled', route đã được duyệt.")
            self.log("  File JSON chứa token agent: chỉ import một lần rồi xoá/cất kỹ. Auth key KHÔNG nằm trong file.")

            def finish():
                self.phase = "done"
                self.primary.configure(text="Cài router khác (Enter)")
                self.vars["auth_key"].set(self.vars["auth_key"].get())  # keep key for the next router
                self.vars["new_password"].set("")
                self.vars["device_id"].set("")
                self._busy(False)

            self.ui(finish)

        self._worker(job)


if __name__ == "__main__":
    window = tk.Tk()
    App(window)
    window.mainloop()
