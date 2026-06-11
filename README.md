Canon Printer Repair Tool

Installation:
1. Extract the first ZIP file you downloaded.
2. Inside that folder, you will see another ZIP file.
3. Extract the second ZIP file as well.
4. After extracting both ZIPs, you will see the program files including:
   - canon_printer_fix.py
   - canon_printer_gui.py
   - start_canon_gui.bat

How to Run:
To run the main repair tool:
   python canon_printer_fix.py
Or double-click it if Python is associated with .py files.

To run the GUI version:
   start_canon_gui.bat

What the Tool Does:
This tool repairs Canon printers by fixing USB bindings, restoring print processors, enabling legacy/RAW modes, restarting the spooler, clearing print queues, and printing test pages. It includes multiple fallback print engines.

Requirements:
No extra pip installs are needed. All required modules are built into Python:
argparse, ctypes, json, os, subprocess, sys, tempfile, textwrap, time, uuid, dataclasses, datetime, pathlib.

After Running:
Once you run canon_printer_fix.py, your Canon printer should come online, bind to the correct USB port, and accept print jobs normally.
