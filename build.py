# -*- coding: utf-8 -*-

import os
import subprocess
import sys

def find_py_file():
    py_files = [f for f in os.listdir() if f.endswith(".py") and f != "build.py"]
    
    if not py_files:
        print("[ERROR] No .py file found in this folder!")
        sys.exit(1)

    if len(py_files) == 1:
        return py_files[0]

    print("[WARNING] Multiple .py files found. Using:", py_files[0])
    return py_files[0]


def install_pyinstaller():
    try:
        import PyInstaller
    except ImportError:
        print("[INFO] Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def build_exe(py_file):
    print(f"[INFO] Building EXE for: {py_file}")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        py_file
    ]

    subprocess.check_call(command)

    print("\n[SUCCESS] Done!")
    print("[INFO] Check the 'dist' folder for your EXE file.")


if __name__ == "__main__":
    install_pyinstaller()
    py_file = find_py_file()
    build_exe(py_file)
