import tkinter as tk
from tkinter import filedialog
import sys
import os

def main():
    # Parent process decodes UTF-8 (paths may contain non-ASCII characters)
    sys.stdout.reconfigure(encoding="utf-8")
    default_path = sys.argv[1] if len(sys.argv) > 1 else ""
    initial_dir = ""
    initial_file = ""
    if default_path:
        # Normalize slashes for Windows/cross-platform
        default_path = default_path.replace("/", os.sep).replace("\\", os.sep)
        initial_dir, initial_file = os.path.split(default_path)

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    file_path = filedialog.asksaveasfilename(
        title="Select Export Location",
        initialdir=initial_dir,
        initialfile=initial_file,
        defaultextension=".mp4",
        filetypes=[("MP4 Video", "*.mp4"), ("MKV Video", "*.mkv")],
        parent=root
    )
    root.destroy()
    if file_path:
        print(file_path, end="")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
