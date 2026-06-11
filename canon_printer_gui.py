import contextlib
import io
import queue
import subprocess
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import canon_printer_fix as repair


APP_DIR = Path(__file__).resolve().parent
CANON_REPAIR_TOOL = APP_DIR / "mypr-win-3_3_0-ea11_2.exe"
ERROR_LOG = APP_DIR / "canon_gui_errors.log"
TEXT_EXTENSIONS = {".txt", ".log", ".md", ".csv", ".tsv", ".py", ".json", ".xml", ".html", ".css", ".js"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff"}


class CanonPrinterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Canon Printer Fix")
        self.minsize(860, 620)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.printers: list[repair.Printer] = []
        self.usb_devices: list[repair.UsbDevice] = []
        self.busy = False

        self.selected_printer = tk.StringVar()
        self.status_text = tk.StringVar(value="Refresh printers to begin.")
        self.admin_text = tk.StringVar(
            value="Administrator: yes" if repair.is_admin() else "Administrator: no"
        )
        self.image_path = tk.StringVar()
        self.image_grayscale = tk.BooleanVar(value=True)
        self.image_dpi = tk.IntVar(value=120)
        self.image_margin = tk.DoubleVar(value=0.25)
        self.text_path = tk.StringVar()

        self.action_buttons: list[ttk.Button] = []

        self._build_ui()
        self.after(100, self._drain_log_queue)
        self.run_task("Refresh printers", self.refresh_printers, disable=False)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        header = ttk.Frame(self, padding=(14, 12, 14, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="Canon Printer Fix", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(header, textvariable=self.admin_text).grid(row=0, column=2, sticky="e")
        ttk.Label(header, textvariable=self.status_text, wraplength=780).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0)
        )

        picker = ttk.LabelFrame(self, text="Printer", padding=12)
        picker.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        picker.columnconfigure(1, weight=1)

        ttk.Label(picker, text="Selected").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.printer_combo = ttk.Combobox(
            picker,
            textvariable=self.selected_printer,
            state="readonly",
            width=48,
        )
        self.printer_combo.grid(row=0, column=1, sticky="ew")
        self.printer_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_status())
        self._add_button(picker, "Refresh", lambda: self.run_task("Refresh printers", self.refresh_printers)).grid(
            row=0, column=2, padx=(8, 0)
        )

        actions = ttk.LabelFrame(self, text="Repair", padding=12)
        actions.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 10))
        for col in range(5):
            actions.columnconfigure(col, weight=1)

        self._add_button(actions, "Text Mode", self.apply_text_mode).grid(
            row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 8)
        )
        self._add_button(actions, "Image Mode", self.apply_image_mode).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=(0, 8)
        )
        self._add_button(actions, "Clear Queue", self.clear_queue).grid(
            row=0, column=2, sticky="ew", padx=(0, 8), pady=(0, 8)
        )
        self._add_button(actions, "Restart Spooler", self.restart_spooler).grid(
            row=0, column=3, sticky="ew", padx=(0, 8), pady=(0, 8)
        )
        self._add_button(actions, "Test Page", self.print_test_page).grid(
            row=0, column=4, sticky="ew", pady=(0, 8)
        )
        self._add_button(actions, "Check USB", self.check_usb).grid(
            row=1, column=0, sticky="ew", padx=(0, 8)
        )

        files = ttk.LabelFrame(self, text="Print From This PC", padding=12)
        files.grid(row=3, column=0, sticky="nsew", padx=14, pady=(0, 10))
        files.columnconfigure(1, weight=1)
        files.rowconfigure(6, weight=1)

        ttk.Label(files, text="Text file").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(files, textvariable=self.text_path).grid(row=0, column=1, sticky="ew")
        self._add_button(files, "Browse", self.browse_text).grid(row=0, column=2, padx=(8, 0))

        self._add_button(files, "Print Text File", self.print_selected_text).grid(
            row=1, column=0, sticky="ew", pady=(8, 0), padx=(0, 8)
        )
        ttk.Label(files, text="Sends through Windows Notepad").grid(
            row=1, column=1, columnspan=2, sticky="w", pady=(8, 0)
        )

        ttk.Separator(files).grid(row=2, column=0, columnspan=3, sticky="ew", pady=12)

        ttk.Label(files, text="Image file").grid(row=3, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(files, textvariable=self.image_path).grid(row=3, column=1, sticky="ew")
        self._add_button(files, "Browse", self.browse_image).grid(row=3, column=2, padx=(8, 0))

        ttk.Checkbutton(files, text="Grayscale", variable=self.image_grayscale).grid(
            row=4, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Label(files, text="Max DPI").grid(row=4, column=1, sticky="e", pady=(10, 0), padx=(0, 8))
        ttk.Spinbox(files, from_=72, to=300, increment=10, textvariable=self.image_dpi, width=8).grid(
            row=4, column=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(files, text="Margin").grid(row=5, column=1, sticky="e", pady=(8, 0), padx=(0, 8))
        ttk.Spinbox(
            files,
            from_=0.0,
            to=1.0,
            increment=0.05,
            textvariable=self.image_margin,
            width=8,
        ).grid(row=5, column=2, sticky="w", pady=(8, 0))

        self._add_button(files, "Print Image File", self.print_selected_image).grid(
            row=5, column=0, sticky="ew", pady=(8, 0), padx=(0, 8)
        )

        log_frame = ttk.LabelFrame(files, text="Log", padding=(8, 8, 8, 8))
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(12, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log = tk.Text(log_frame, height=14, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        footer = ttk.Frame(self, padding=(14, 0, 14, 12))
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text="Use Print File for one-click direct printing from this app.",
        ).grid(row=0, column=0, sticky="w")
        self._add_button(footer, "Print File...", self.print_any_file).grid(
            row=0, column=1, sticky="e", padx=(0, 8)
        )
        self._add_button(footer, "Open Canon Repair Tool", self.open_canon_repair_tool).grid(
            row=0, column=2, sticky="e"
        )

    def _add_button(self, parent: tk.Widget, text: str, command) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command)
        self.action_buttons.append(button)
        return button

    def run_task(self, title: str, callback, disable: bool = True) -> None:
        if self.busy and disable:
            return

        if disable:
            self.busy = True
            self._set_buttons_enabled(False)
        self.log_message(f"\n== {title} ==\n")

        def worker() -> None:
            stream = io.StringIO()
            try:
                with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                    callback()
            except SystemExit as exc:
                print(exc, file=stream)
            except Exception as exc:
                print(f"Unexpected error: {exc}", file=stream)
                print(f"Details were written to: {ERROR_LOG}", file=stream)
                with ERROR_LOG.open("a", encoding="utf-8") as error_log:
                    error_log.write(f"\n== {title} ==\n")
                    traceback.print_exc(file=error_log)
            finally:
                output = stream.getvalue().strip()
                if output:
                    self.log_queue.put(output + "\n")
                self.log_queue.put("__TASK_DONE__" if disable else "__TASK_REFRESH_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.action_buttons:
            button.configure(state=state)
        self.printer_combo.configure(state="readonly" if enabled else "disabled")

    def _drain_log_queue(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                if item in {"__TASK_DONE__", "__TASK_REFRESH_DONE__"}:
                    if item == "__TASK_DONE__":
                        self.busy = False
                        self._set_buttons_enabled(True)
                    self.update_status()
                    continue
                self.log_message(item)
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def log_message(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_printers(self) -> None:
        self.printers = repair.get_printers()
        self.usb_devices = repair.get_canon_usb_devices()
        names = [printer.name for printer in self.printers]
        self.after(0, lambda: self._set_printer_names(names))

    def _set_printer_names(self, names: list[str]) -> None:
        self.printer_combo.configure(values=names)
        current = self.selected_printer.get()
        if current not in names:
            default = next((printer.name for printer in self.printers if printer.default), "")
            canon = next(
                (
                    printer.name
                    for printer in self.printers
                    if "canon" in printer.name.casefold() or "canon" in printer.driver.casefold()
                ),
                "",
            )
            self.selected_printer.set(default or canon or (names[0] if names else ""))
        self.update_status()

    def selected(self) -> repair.Printer:
        printers = repair.get_printers()
        requested = self.selected_printer.get().strip() or None
        return repair.find_printer(printers, requested)

    def refresh_after_action(self) -> repair.Printer:
        printer = self.selected()
        self.after(0, lambda: self.selected_printer.set(printer.name))
        self.refresh_printers()
        return printer

    def update_status(self) -> None:
        name = self.selected_printer.get()
        printer = next((item for item in self.printers if item.name == name), None)
        if not printer and name:
            try:
                printer = repair.find_printer(repair.get_printers(), name)
            except SystemExit:
                printer = None
        if printer:
            default = "default" if printer.default else "not default"
            offline = "offline" if printer.offline else "online"
            if self.usb_devices:
                usb = "Canon USB device detected"
            elif repair.printer_has_usb_port(printer):
                usb = f"queue on {printer.port}"
            else:
                usb = "no USB printer port"
            self.status_text.set(
                f"{printer.name} | {printer.status} | {offline} | {default} | "
                f"port {printer.port} | {usb} | driver {printer.driver}"
            )
        else:
            self.status_text.set("No printer selected.")

    def check_usb(self) -> None:
        self.run_task("Check Canon USB", self._check_usb)

    def _check_usb(self) -> None:
        devices = repair.get_canon_usb_devices()
        self.usb_devices = devices
        if not devices:
            printer = self.selected()
            if repair.printer_has_usb_port(printer):
                print(f"No separate USBPRINT device is visible, but the queue is using {printer.port}.")
                print("That can happen with this older Canon driver on newer Windows builds.")
                print("If the test page prints, use the print buttons anyway.")
            else:
                print("No physical Canon USB printer device is detected.")
                print("The Windows queue exists, but the printer is not answering on USB.")
                print("Turn the printer on, unplug/replug USB, try another USB port, then click Refresh.")
        else:
            for device in devices:
                print(f"{device.name}")
                print(f"  status={device.status}, error={device.error_code}")
                print(f"  id={device.device_id}")
        self.refresh_printers()

    def apply_text_mode(self) -> None:
        self.run_task("Apply text mode", self._apply_text_mode)

    def _apply_text_mode(self) -> None:
        printer = self.selected()
        repair.set_default_printer(printer)
        repair.fix_usb_port(printer)
        printer = self.selected()
        repair.fix_print_processor(printer)
        printer = self.selected()
        repair.enable_legacy_usb_mode(printer)
        repair.set_online(printer)
        repair.restart_spooler()
        self.refresh_after_action()

    def apply_image_mode(self) -> None:
        self.run_task("Apply image mode", self._apply_image_mode)

    def _apply_image_mode(self) -> None:
        printer = self.selected()
        repair.set_default_printer(printer)
        repair.fix_usb_port(printer)
        printer = self.selected()
        repair.restore_canon_print_processor(printer)
        printer = self.selected()
        repair.enable_graphics_mode(printer)
        repair.set_online(printer)
        repair.restart_spooler()
        self.refresh_after_action()

    def clear_queue(self) -> None:
        self.run_task("Clear queue", self._clear_queue)

    def _clear_queue(self) -> None:
        repair.clear_queue(self.selected())
        self.refresh_after_action()

    def restart_spooler(self) -> None:
        self.run_task("Restart spooler", self._restart_spooler)

    def _restart_spooler(self) -> None:
        repair.restart_spooler()
        self.refresh_after_action()

    def print_test_page(self) -> None:
        self.run_task("Print test page", self._print_test_page)

    def _print_test_page(self) -> None:
        repair.print_test_page(self.selected())
        self.refresh_after_action()

    def browse_image(self) -> None:
        filename = filedialog.askopenfilename(
            title="Choose an image",
            filetypes=[
                ("Image files", "*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff"),
                ("All files", "*.*"),
            ],
        )
        if filename:
            self.image_path.set(filename)

    def browse_text(self) -> None:
        filename = filedialog.askopenfilename(
            title="Choose a text file",
            filetypes=[
                ("Text files", "*.txt *.log *.md *.csv *.tsv *.py *.json *.xml *.html *.css *.js"),
                ("All files", "*.*"),
            ],
        )
        if filename:
            self.text_path.set(filename)

    def print_any_file(self) -> None:
        filename = filedialog.askopenfilename(
            title="Choose a file to print",
            filetypes=[
                ("Text and image files", "*.txt *.log *.md *.csv *.tsv *.py *.json *.xml *.html *.css *.js *.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff"),
                ("Text files", "*.txt *.log *.md *.csv *.tsv *.py *.json *.xml *.html *.css *.js"),
                ("Image files", "*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff"),
                ("All files", "*.*"),
            ],
        )
        if not filename:
            return

        path = Path(filename)
        suffix = path.suffix.casefold()
        if suffix in IMAGE_EXTENSIONS:
            self.image_path.set(filename)
            self.run_task("Print image file", self._print_selected_image)
        elif suffix in TEXT_EXTENSIONS:
            self.text_path.set(filename)
            self.run_task("Print text file", self._print_selected_text)
        else:
            messagebox.showwarning(
                "Unsupported File",
                "This app can directly print text files and image files.",
            )

    def print_selected_text(self) -> None:
        if not self.text_path.get().strip():
            messagebox.showwarning("Choose Text File", "Pick a text file first.")
            return
        self.run_task("Print text file", self._print_selected_text)

    def _print_selected_text(self) -> None:
        printer = self.selected()
        repair.set_default_printer(printer)
        repair.clear_queue(printer)
        repair.fix_usb_port(printer)
        printer = self.selected()
        repair.fix_print_processor(printer)
        printer = self.selected()
        repair.enable_legacy_usb_mode(printer)
        repair.set_online(printer)
        repair.restart_spooler()
        printer = self.selected()
        repair.print_text_with_windows_app(printer, Path(self.text_path.get()))
        self.refresh_after_action()

    def print_selected_image(self) -> None:
        if not self.image_path.get().strip():
            messagebox.showwarning("Choose Image", "Pick an image file first.")
            return
        self.run_task("Print selected image", self._print_selected_image)

    def _print_selected_image(self) -> None:
        printer = self.selected()
        repair.set_default_printer(printer)
        repair.clear_queue(printer)
        repair.fix_usb_port(printer)
        printer = self.selected()
        repair.restore_canon_print_processor(printer)
        printer = self.selected()
        repair.enable_graphics_mode(printer)
        repair.set_online(printer)
        repair.restart_spooler()
        printer = self.selected()
        repair.print_image_with_windows_app(
            printer,
            Path(self.image_path.get()),
            grayscale=self.image_grayscale.get(),
            max_dpi=max(72, self.image_dpi.get()),
        )
        self.refresh_after_action()

    def open_canon_repair_tool(self) -> None:
        if not CANON_REPAIR_TOOL.exists():
            messagebox.showerror(
                "Canon Repair Tool Missing",
                f"Could not find:\n{CANON_REPAIR_TOOL}",
            )
            return
        self.log_message(f"\nOpening Canon repair tool:\n{CANON_REPAIR_TOOL}\n")
        try:
            subprocess.Popen([str(CANON_REPAIR_TOOL)], cwd=str(APP_DIR))
        except OSError as exc:
            messagebox.showerror("Canon Repair Tool", f"Could not open Canon repair tool:\n{exc}")


def main() -> None:
    app = CanonPrinterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
