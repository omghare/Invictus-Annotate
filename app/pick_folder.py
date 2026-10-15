import tkinter as tk
from tkinter import filedialog
import sys

def main():
    # Parent process decodes UTF-8 (paths may contain non-ASCII characters)
    sys.stdout.reconfigure(encoding="utf-8")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    folder = filedialog.askdirectory(title="Select Image Folder", parent=root)
    root.destroy()
    if folder:
        print(folder, end="")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
