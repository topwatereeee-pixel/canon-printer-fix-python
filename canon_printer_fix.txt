import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class Printer:
    name: str
    driver: str
    port: str
    status: str
    offline: bool
    default: bool


@dataclass
class PrinterPort:
    name: str
    description: str
    monitor: str


@dataclass
class UsbDevice:
    name: str
    status: str
    device_id: str
    error_code: int


@dataclass
class PrintJob:
    id: int
    document: str
    status: str
    datatype: str
    size: int


def run_powershell(script: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        text=True,
        capture_output=True,
        check=check,
    )


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def json_list(output: str) -> list[dict]:
    output = output.strip()
    if not output:
        return []

    data = json.loads(output)
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def get_printers() -> list[Printer]:
    script = """
$statusNames = @{
    1 = "Other"
    2 = "Unknown"
    3 = "Idle"
    4 = "Printing"
    5 = "Warmup"
    6 = "Stopped Printing"
    7 = "Offline"
}
$printers = Get-CimInstance Win32_Printer | ForEach-Object {
    [pscustomobject]@{
        Name = $_.Name
        DriverName = $_.DriverName
        PortName = $_.PortName
        PrinterStatus = $statusNames[[int]$_.PrinterStatus]
        WorkOffline = [bool]$_.WorkOffline
        Default = [bool]$_.Default
    }
}
$printers | ConvertTo-Json -Depth 3
"""
    result = run_powershell(script)
    output = result.stdout.strip()
    if not output:
        return []

    return [
        Printer(
            name=item.get("Name", ""),
            driver=item.get("DriverName", ""),
            port=item.get("PortName", ""),
            status=str(item.get("PrinterStatus", "")),
            offline=bool(item.get("WorkOffline", False)),
            default=bool(item.get("Default", False)),
        )
        for item in json_list(output)
    ]


def get_printer_ports() -> list[PrinterPort]:
    script = """
Get-PrinterPort |
    Select-Object Name,Description,PortMonitor |
    ConvertTo-Json -Depth 3
"""
    result = run_powershell(script)
    output = result.stdout.strip()
    if not output:
        return []

    return [
        PrinterPort(
            name=item.get("Name", ""),
            description=item.get("Description", "") or "",
            monitor=item.get("PortMonitor", "") or "",
        )
        for item in json_list(output)
    ]


def get_canon_usb_devices() -> list[UsbDevice]:
    script = """
$devices = Get-CimInstance Win32_PnPEntity |
    Where-Object {
        $_.PNPDeviceID -like 'USBPRINT\\CANON*' -or
        $_.PNPDeviceID -like 'USB\\VID_04A9*'
    } |
    Select-Object Name,Status,PNPDeviceID,ConfigManagerErrorCode
$devices | ConvertTo-Json -Depth 3
"""
    result = run_powershell(script, check=False)
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        return []

    return [
        UsbDevice(
            name=item.get("Name", ""),
            status=item.get("Status", "") or "",
            device_id=item.get("PNPDeviceID", "") or "",
            error_code=int(item.get("ConfigManagerErrorCode", 0) or 0),
        )
        for item in json_list(output)
    ]


def canon_usb_connected() -> bool:
    return any(device.status == "OK" and device.error_code == 0 for device in get_canon_usb_devices())


def printer_has_usb_port(printer: Printer) -> bool:
    return printer.port.upper().startswith("USB")


def find_printer(printers: list[Printer], requested_name: str | None) -> Printer:
    if requested_name:
        requested = requested_name.casefold()
        for printer in printers:
            if printer.name.casefold() == requested:
                return printer
        for printer in printers:
            if requested in printer.name.casefold():
                return printer
        raise SystemExit(f"No printer matched {requested_name!r}. Run with --list to see installed printers.")

    canon_printers = [
        printer
        for printer in printers
        if "canon" in printer.name.casefold() or "canon" in printer.driver.casefold()
    ]
    if not canon_printers:
        raise SystemExit("No Canon printer was found. Run with --list to see installed printers.")

    default_canon = next((printer for printer in canon_printers if printer.default), None)
    return default_canon or canon_printers[0]


def model_tokens(value: str) -> set[str]:
    cleaned = "".join(char.lower() if char.isalnum() else " " for char in value)
    return {token for token in cleaned.split() if len(token) >= 3}


def find_best_usb_port(printer: Printer, ports: list[PrinterPort]) -> PrinterPort | None:
    printer_tokens = model_tokens(printer.name) | model_tokens(printer.driver)
    usb_ports = [port for port in ports if port.name.upper().startswith("USB")]
    if not usb_ports:
        return None

    canon_usb_ports = [
        port
        for port in usb_ports
        if "canon" in f"{port.description} {port.monitor}".casefold()
    ]
    if len(canon_usb_ports) == 1:
        return canon_usb_ports[0]

    scored_ports: list[tuple[int, PrinterPort]] = []
    for port in usb_ports:
        text_tokens = model_tokens(f"{port.name} {port.description} {port.monitor}")
        score = len(printer_tokens & text_tokens)
        if score:
            scored_ports.append((score, port))

    if scored_ports:
        scored_ports.sort(key=lambda item: item[0], reverse=True)
        return scored_ports[0][1]

    return usb_ports[0] if len(usb_ports) == 1 else None


def set_default_printer(printer: Printer) -> None:
    quoted_name = powershell_quote(printer.name)
    script = f"""
$network = New-Object -ComObject WScript.Network
$network.SetDefaultPrinter({quoted_name})
"""
    run_powershell(script)
    print(f"Set default printer: {printer.name}")


def fix_usb_port(printer: Printer) -> None:
    ports = get_printer_ports()
    usb_port = find_best_usb_port(printer, ports)
    if not usb_port:
        print("No matching USB printer port was found.")
        return

    if printer.port.casefold() == usb_port.name.casefold():
        print(f"Printer is already using USB port: {usb_port.name}")
        return

    quoted_name = powershell_quote(printer.name)
    quoted_port = powershell_quote(usb_port.name)
    script = f"Set-Printer -Name {quoted_name} -PortName {quoted_port}"
    result = run_powershell(script, check=False)
    if result.returncode == 0:
        print(f"Changed printer port: {printer.port} -> {usb_port.name} ({usb_port.description})")
        return

    print("Could not change the printer port. Re-run Command Prompt as Administrator and try again.")
    if result.stderr.strip():
        print(result.stderr.strip())


def fix_print_processor(printer: Printer) -> None:
    quoted_name = powershell_quote(printer.name)
    script = f"Set-Printer -Name {quoted_name} -PrintProcessor 'WinPrint' -Datatype 'RAW'"
    result = run_powershell(script, check=False)
    if result.returncode == 0:
        print("Changed print processor to WinPrint with RAW data type.")
        return

    print("Could not change the print processor. Re-run Command Prompt as Administrator and try again.")
    if result.stderr.strip():
        print(result.stderr.strip())


def restore_canon_print_processor(printer: Printer) -> None:
    quoted_name = powershell_quote(printer.name)
    quoted_processor = powershell_quote(f"{printer.driver.replace(' Printer', '')} Print Processor")
    script = f"Set-Printer -Name {quoted_name} -PrintProcessor {quoted_processor} -Datatype 'RAW'"
    result = run_powershell(script, check=False)
    if result.returncode == 0:
        print(f"Changed print processor to Canon driver processor: {quoted_processor.strip(chr(39))}")
        return

    print("Could not restore the Canon print processor. Re-run Command Prompt as Administrator and try again.")
    if result.stderr.strip():
        print(result.stderr.strip())


def enable_legacy_usb_mode(printer: Printer) -> None:
    quoted_name = powershell_quote(printer.name)
    script = f"""
rundll32 printui.dll,PrintUIEntry /Xs /n {quoted_name} attributes +direct
rundll32 printui.dll,PrintUIEntry /Xs /n {quoted_name} attributes +RawOnly
"""
    result = run_powershell(script, check=False)
    if result.returncode == 0:
        print("Enabled legacy USB mode: direct printing and RAW-only data.")
        return

    print("Could not enable legacy USB mode. Re-run Command Prompt as Administrator and try again.")
    if result.stderr.strip():
        print(result.stderr.strip())


def enable_graphics_mode(printer: Printer) -> None:
    quoted_name = powershell_quote(printer.name)
    script = f"""
rundll32 printui.dll,PrintUIEntry /Xs /n {quoted_name} attributes -RawOnly
rundll32 printui.dll,PrintUIEntry /Xs /n {quoted_name} attributes -direct
rundll32 printui.dll,PrintUIEntry /Xs /n {quoted_name} attributes +queued
"""
    result = run_powershell(script, check=False)
    if result.returncode == 0:
        print("Enabled graphics mode: normal spooling and non-RAW-only jobs.")
        return

    print("Could not enable graphics mode. Re-run Command Prompt as Administrator and try again.")
    if result.stderr.strip():
        print(result.stderr.strip())


def set_online(printer: Printer) -> None:
    quoted_name = powershell_quote(printer.name)
    script = f"""
$printer = Get-CimInstance Win32_Printer | Where-Object Name -eq {quoted_name} | Select-Object -First 1
if (-not $printer) {{
    throw "Printer not found."
}}
if ($printer.WorkOffline) {{
    Set-CimInstance -InputObject $printer -Property @{{ WorkOffline = $false }} | Out-Null
    "Changed printer from offline to online."
}} else {{
    "Printer already reports online."
}}
"""
    result = run_powershell(script, check=False)
    if result.returncode == 0:
        print(result.stdout.strip())
    else:
        print("Could not change the offline setting. Open the printer queue and uncheck 'Use Printer Offline'.")


def get_print_jobs(printer: Printer) -> list[PrintJob]:
    quoted_name = powershell_quote(printer.name)
    script = f"""
Get-PrintJob -PrinterName {quoted_name} -ErrorAction SilentlyContinue |
    Select-Object ID,DocumentName,JobStatus,Datatype,Size |
    ConvertTo-Json -Depth 3
"""
    result = run_powershell(script, check=False)
    if result.returncode != 0:
        return []

    jobs: list[PrintJob] = []
    for item in json_list(result.stdout):
        jobs.append(
            PrintJob(
                id=int(item.get("ID", 0) or 0),
                document=item.get("DocumentName", "") or "",
                status=str(item.get("JobStatus", "") or ""),
                datatype=item.get("Datatype", "") or "",
                size=int(item.get("Size", 0) or 0),
            )
        )
    return jobs


def wait_for_queue_empty(printer: Printer, timeout: int = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not get_print_jobs(printer):
            return True
        time.sleep(1)
    return not get_print_jobs(printer)


def restart_spooler() -> None:
    script = """
try {
    Restart-Service -Name Spooler -Force -ErrorAction Stop
    Start-Sleep -Seconds 2
    "Restarted print spooler. Status: " + (Get-Service -Name Spooler).Status
} catch {
    "Could not restart the print spooler: " + $_.Exception.Message
    "Current spooler status: " + (Get-Service -Name Spooler).Status
}
"""
    result = run_powershell(script, check=False)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())


def clear_queue(printer: Printer) -> None:
    quoted_name = powershell_quote(printer.name)
    script = f"""
$jobs = Get-PrintJob -PrinterName {quoted_name} -ErrorAction SilentlyContinue
if ($jobs) {{
    $jobs | Remove-PrintJob
    "Cleared " + @($jobs).Count + " queued print job(s)."
}} else {{
    "No queued print jobs found."
}}
"""
    result = run_powershell(script, check=False)
    print((result.stdout or result.stderr).strip())
    if not wait_for_queue_empty(printer, timeout=8):
        print("Some print jobs are still deleting. Trying a spooler refresh.")
        restart_spooler()
        if wait_for_queue_empty(printer, timeout=8):
            print("Queue is now clear.")
        else:
            print("Queue still has retained jobs. Open the queue and cancel them manually if printing remains stuck.")


def print_test_page(printer: Printer) -> None:
    result = subprocess.run(
        ["rundll32.exe", "printui.dll,PrintUIEntry", "/k", "/n", printer.name],
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        print(f"Sent Windows test page to: {printer.name}")
    else:
        print("Could not send a Windows test page.")
        if result.stderr.strip():
            print(result.stderr.strip())


def system_exe(name: str) -> str:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidate = system_root / "System32" / name
    return str(candidate if candidate.exists() else name)


def report_queue_after_submit(printer: Printer, timeout: int = 8) -> None:
    deadline = time.monotonic() + timeout
    last_jobs: list[PrintJob] = []
    while time.monotonic() < deadline:
        last_jobs = get_print_jobs(printer)
        if any("Error" in job.status for job in last_jobs):
            break
        if not last_jobs:
            time.sleep(1)
            continue
        time.sleep(1)

    jobs = get_print_jobs(printer) or last_jobs
    if not jobs:
        print("No jobs are currently stuck in the queue.")
        return

    print("Current queue:")
    for job in jobs:
        print(f"  #{job.id}: {job.document} | {job.status or 'Normal'} | {job.datatype} | {job.size} bytes")


def list_printers(printers: list[Printer]) -> None:
    if not printers:
        print("No printers are installed.")
        return

    for printer in printers:
        marker = "default" if printer.default else "installed"
        offline = "offline" if printer.offline else "online"
        print(f"- {printer.name} ({marker}, {offline}, status={printer.status})")
        print(f"  driver={printer.driver}")
        print(f"  port={printer.port}")


def print_image(
    printer: Printer,
    image_path: Path,
    grayscale: bool,
    max_dpi: int,
    margin_inches: float,
) -> None:
    try:
        import win32print
        import win32ui
        from PIL import Image, ImageOps, ImageWin
    except ImportError as exc:
        raise SystemExit(f"Image printing needs Pillow and pywin32 installed: {exc}") from exc

    if not image_path.exists():
        raise SystemExit(f"Image not found: {image_path}")

    # DeviceCaps constants from wingdi.h.
    horzres = 8
    vertres = 10
    logpixelsx = 88
    logpixelsy = 90

    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        image = background
    image = image.convert("L" if grayscale else "RGB")

    printer_name = printer.name
    win32print.SetDefaultPrinter(printer_name)
    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer_name)

    try:
        page_width = hdc.GetDeviceCaps(horzres)
        page_height = hdc.GetDeviceCaps(vertres)
        dpi_x = hdc.GetDeviceCaps(logpixelsx)
        dpi_y = hdc.GetDeviceCaps(logpixelsy)

        margin_x = max(0, int(margin_inches * dpi_x))
        margin_y = max(0, int(margin_inches * dpi_y))
        max_width = max(1, page_width - (margin_x * 2))
        max_height = max(1, page_height - (margin_y * 2))

        original_width, original_height = image.size
        page_scale = min(max_width / original_width, max_height / original_height)
        target_width = max(1, int(original_width * page_scale))
        target_height = max(1, int(original_height * page_scale))

        # Old Canon drivers are much happier when the source raster is modest,
        # even if Windows scales that raster up to the page target rectangle.
        target_width_inches = target_width / max(1, dpi_x)
        target_height_inches = target_height / max(1, dpi_y)
        raster_width = max(1, int(target_width_inches * max_dpi))
        raster_height = max(1, int(target_height_inches * max_dpi))
        image.thumbnail((raster_width, raster_height), Image.Resampling.LANCZOS)

        left = margin_x + ((max_width - target_width) // 2)
        top = margin_y + ((max_height - target_height) // 2)
        rect = (left, top, left + target_width, top + target_height)

        document_name = f"Canon image print: {image_path.name}"
        hdc.StartDoc(document_name)
        hdc.StartPage()
        ImageWin.Dib(image).draw(hdc.GetHandleOutput(), rect)
        hdc.EndPage()
        hdc.EndDoc()
    finally:
        hdc.DeleteDC()

    color_mode = "grayscale" if grayscale else "color"
    print(f"Sent image to {printer_name}: {image_path}")
    print(f"Prepared as {image.width}x{image.height} pixels, {color_mode}, max {max_dpi} DPI.")


def read_text_file(text_path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return text_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return text_path.read_text(errors="replace")


def print_text_file(
    printer: Printer,
    text_path: Path,
    font_name: str,
    font_size: int,
    margin_inches: float,
) -> None:
    try:
        import win32print
        import win32ui
    except ImportError as exc:
        raise SystemExit(f"Text printing needs pywin32 installed: {exc}") from exc

    if not text_path.exists():
        raise SystemExit(f"Text file not found: {text_path}")

    text = read_text_file(text_path)

    # DeviceCaps constants from wingdi.h.
    horzres = 8
    vertres = 10
    logpixelsx = 88
    logpixelsy = 90

    win32print.SetDefaultPrinter(printer.name)
    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer.name)

    try:
        page_width = hdc.GetDeviceCaps(horzres)
        page_height = hdc.GetDeviceCaps(vertres)
        dpi_x = hdc.GetDeviceCaps(logpixelsx)
        dpi_y = hdc.GetDeviceCaps(logpixelsy)

        margin_x = max(0, int(margin_inches * dpi_x))
        margin_y = max(0, int(margin_inches * dpi_y))
        printable_width = max(1, page_width - (margin_x * 2))
        printable_bottom = max(1, page_height - margin_y)

        font_height = -max(1, int(font_size * dpi_y / 72))
        font = win32ui.CreateFont(
            {
                "name": font_name,
                "height": font_height,
                "weight": 400,
            }
        )
        hdc.SelectObject(font)

        sample_width, sample_height = hdc.GetTextExtent("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
        average_char_width = max(1, sample_width // 52)
        max_chars = max(20, printable_width // average_char_width)
        line_height = max(1, hdc.GetTextExtent("Ag")[1] + int(dpi_y * 0.03))

        lines: list[str] = []
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            expanded = raw_line.expandtabs(4)
            if not expanded:
                lines.append("")
                continue
            lines.extend(
                textwrap.wrap(
                    expanded,
                    width=max_chars,
                    break_long_words=True,
                    replace_whitespace=False,
                    drop_whitespace=False,
                )
                or [""]
            )

        document_name = f"Canon text print: {text_path.name}"
        hdc.StartDoc(document_name)
        hdc.StartPage()
        y = margin_y
        pages = 1

        for line in lines:
            if y + line_height > printable_bottom:
                hdc.EndPage()
                hdc.StartPage()
                y = margin_y
                pages += 1
            hdc.TextOut(margin_x, y, line)
            y += line_height

        hdc.EndPage()
        hdc.EndDoc()
    finally:
        hdc.DeleteDC()

    print(f"Sent text file to {printer.name}: {text_path}")
    print(f"Printed {len(lines)} wrapped line(s) using {font_name} {font_size}pt.")


def wait_for_print_command(process: subprocess.Popen[str], app_name: str, timeout: int = 45) -> bool:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"{app_name} is still running after {timeout} seconds.")
        print("The print command was started; check the printer queue if nothing prints.")
        return False

    if process.returncode not in (0, None):
        print(f"{app_name} exited with code {process.returncode}.")
    if stdout and stdout.strip():
        print(stdout.strip())
    if stderr and stderr.strip():
        print(stderr.strip())
    return True


def print_text_with_windows_app(printer: Printer, text_path: Path) -> None:
    if not text_path.exists():
        raise SystemExit(f"Text file not found: {text_path}")

    set_default_printer(printer)
    process = subprocess.Popen(
        [system_exe("notepad.exe"), "/pt", str(text_path), printer.name, printer.driver, printer.port],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for_print_command(process, "Notepad")
    print(f"Sent text file through Notepad: {text_path}")
    report_queue_after_submit(printer)


def prepare_image_for_windows_app(image_path: Path, grayscale: bool, max_dpi: int) -> Path:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return image_path

    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        image = background

    image = image.convert("L" if grayscale else "RGB")
    max_side = max(600, min(2400, max_dpi * 8))
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    temp_dir = Path(tempfile.gettempdir()) / "canon_printer_fix"
    temp_dir.mkdir(exist_ok=True)
    prepared_path = temp_dir / f"{image_path.stem}_print_{uuid.uuid4().hex[:8]}.jpg"
    image.save(prepared_path, "JPEG", quality=88, optimize=True)
    mode = "grayscale" if grayscale else "color"
    print(f"Prepared image for Windows printing: {prepared_path}")
    print(f"Prepared as {image.width}x{image.height}, {mode}.")
    return prepared_path


def cleanup_prepared_images(max_age_hours: int = 24) -> None:
    temp_dir = Path(tempfile.gettempdir()) / "canon_printer_fix"
    if not temp_dir.exists():
        return

    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    for file_path in temp_dir.glob("*_print_*.jpg"):
        try:
            modified = datetime.fromtimestamp(file_path.stat().st_mtime)
            if modified < cutoff:
                file_path.unlink()
        except OSError:
            pass


def print_image_with_windows_app(
    printer: Printer,
    image_path: Path,
    grayscale: bool = True,
    max_dpi: int = 120,
) -> None:
    if not image_path.exists():
        raise SystemExit(f"Image not found: {image_path}")

    cleanup_prepared_images()
    prepared_path = prepare_image_for_windows_app(image_path, grayscale, max_dpi)
    set_default_printer(printer)
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    image_viewer = system_root / "System32" / "shimgvw.dll"
    if not image_viewer.exists():
        raise SystemExit(f"Windows image print handler was not found: {image_viewer}")

    process = subprocess.Popen(
        [
            system_exe("rundll32.exe"),
            f"{image_viewer},ImageView_PrintTo",
            "/pt",
            str(prepared_path),
            printer.name,
            printer.driver,
            printer.port,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    finished = wait_for_print_command(process, "Windows image print handler", timeout=60)
    time.sleep(2)
    print(f"Sent image through Windows image print handler: {image_path}")
    if finished and prepared_path != image_path:
        print(f"Kept prepared print file until Windows finishes with it: {prepared_path}")
    report_queue_after_submit(printer)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find and repair a Canon printer using Windows printer tools."
    )
    parser.add_argument("--printer", help="Printer name or partial name. Defaults to the first Canon printer.")
    parser.add_argument("--list", action="store_true", help="List installed printers and exit.")
    parser.add_argument("--clear-queue", action="store_true", help="Clear stuck jobs for the selected printer.")
    parser.add_argument("--fix-usb-port", action="store_true", help="Move the selected Canon printer to its matching USB port.")
    parser.add_argument("--fix-print-processor", action="store_true", help="Use Windows WinPrint instead of the Canon print processor.")
    parser.add_argument("--canon-print-processor", action="store_true", help="Use Canon's own print processor.")
    parser.add_argument("--graphics-mode", action="store_true", help="Use normal spooling for images and graphics.")
    parser.add_argument("--legacy-usb-mode", action="store_true", help="Use direct RAW printing for plain text jobs.")
    parser.add_argument("--fix-all", action="store_true", help="Apply the USB port, WinPrint, and legacy USB repairs.")
    parser.add_argument("--fix-images", action="store_true", help="Apply the USB port, Canon print processor, and graphics-mode repairs.")
    parser.add_argument("--restart-spooler", action="store_true", help="Restart the Windows print spooler.")
    parser.add_argument("--test-page", action="store_true", help="Send a Windows printer test page.")
    parser.add_argument("--no-default", action="store_true", help="Do not set the Canon printer as default.")
    parser.add_argument("--print-image", type=Path, help="Print an image through a downscaled GDI path.")
    parser.add_argument("--image-color", action="store_true", help="Print image in color. Defaults to grayscale.")
    parser.add_argument("--image-max-dpi", type=int, default=200, help="Maximum image DPI for --print-image.")
    parser.add_argument("--image-margin", type=float, default=0.25, help="Page margin in inches for --print-image.")
    parser.add_argument("--print-text", type=Path, help="Print a text file directly.")
    parser.add_argument("--app-print", action="store_true", help="Use Windows apps for file printing.")
    parser.add_argument("--text-font", default="Consolas", help="Font for --print-text.")
    parser.add_argument("--text-size", type=int, default=10, help="Font size for --print-text.")
    parser.add_argument("--text-margin", type=float, default=0.5, help="Page margin in inches for --print-text.")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("This helper is for Windows only.")

    printers = get_printers()
    if args.list:
        list_printers(printers)
        return 0

    printer = find_printer(printers, args.printer)
    print(f"Selected printer: {printer.name}")
    print(f"Driver: {printer.driver}")
    print(f"Port: {printer.port}")
    print(f"Status: {printer.status}")
    print(f"Offline flag: {printer.offline}")

    if not args.no_default:
        set_default_printer(printer)

    if args.fix_all:
        args.fix_usb_port = True
        args.fix_print_processor = True
        args.legacy_usb_mode = True
        args.restart_spooler = True

    if args.fix_images:
        args.fix_usb_port = True
        args.canon_print_processor = True
        args.graphics_mode = True
        args.restart_spooler = True

    if args.app_print and args.print_text and args.print_image:
        raise SystemExit("Use one --app-print file at a time: either --print-text or --print-image.")

    if args.app_print and args.print_text:
        args.clear_queue = True
        args.fix_usb_port = True
        args.fix_print_processor = True
        args.legacy_usb_mode = True

    if args.app_print and args.print_image:
        args.clear_queue = True
        args.fix_usb_port = True
        args.canon_print_processor = True
        args.graphics_mode = True

    if args.print_image:
        if not args.app_print:
            args.graphics_mode = True
    if args.print_text:
        if not args.app_print:
            args.legacy_usb_mode = True

    if args.fix_usb_port:
        fix_usb_port(printer)
        printer = find_printer(get_printers(), printer.name)

    if args.fix_print_processor:
        fix_print_processor(printer)
        printer = find_printer(get_printers(), printer.name)

    if args.canon_print_processor:
        restore_canon_print_processor(printer)
        printer = find_printer(get_printers(), printer.name)

    if args.legacy_usb_mode:
        enable_legacy_usb_mode(printer)
        printer = find_printer(get_printers(), printer.name)

    if args.graphics_mode:
        enable_graphics_mode(printer)
        printer = find_printer(get_printers(), printer.name)

    set_online(printer)

    if args.clear_queue:
        clear_queue(printer)

    if args.restart_spooler:
        restart_spooler()
    else:
        print("Skipped spooler restart. Add --restart-spooler if jobs stay stuck.")

    if args.test_page:
        print_test_page(printer)
    else:
        print("Skipped test page. Add --test-page when paper is loaded.")

    if args.print_image:
        if args.app_print:
            print_image_with_windows_app(printer, args.print_image)
        else:
            print_image(
                printer,
                args.print_image,
                grayscale=not args.image_color,
                max_dpi=max(72, args.image_max_dpi),
                margin_inches=max(0, args.image_margin),
            )

    if args.print_text:
        if args.app_print:
            print_text_with_windows_app(printer, args.print_text)
        else:
            print_text_file(
                printer,
                args.print_text,
                font_name=args.text_font,
                font_size=max(6, args.text_size),
                margin_inches=max(0, args.text_margin),
            )

    if not is_admin():
        print("Note: some repair actions need Administrator permissions.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
