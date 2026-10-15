import tkinter as tk
from tkinter import filedialog
import sys

def main():
    # Parent process decodes UTF-8 (paths may contain non-ASCII characters)
    sys.stdout.reconfigure(encoding="utf-8")
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()

    if mode == "--yaml":
        file_path = filedialog.askopenfilename(
            title="Select data.yaml",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
            parent=root,
        )
    else:
        file_path = filedialog.askopenfilename(
            title="Select Video to Import",
            filetypes=[("Video Files", "*.mp4 *.mkv *.avi *.mov")],
            parent=root,
        )

    root.destroy()
    if file_path:
        print(file_path, end="")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
