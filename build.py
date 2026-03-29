import os
import subprocess
import sys

def find_py_file():
    # Get all .py files in current folder
    py_files = [f for f in os.listdir() if f.endswith(".py") and f != "build.py"]
    
    if not py_files:
        print("❌ No .py file found in this folder!")
        sys.exit()

    if len(py_files) == 1:
        return py_files[0]

    # If multiple files exist, pick the first one
    print("⚠ Multiple .py files found. Using:", py_files[0])
    return py_files[0]


def install_pyinstaller():
    try:
        import pyinstaller
    except ImportError:
        print("📦 Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def build_exe(py_file):
    print(f"🚀 Building EXE for: {py_file}")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        py_file
    ]

    subprocess.run(command)

    print("\n✅ Done!")
    print("📁 Check the 'dist' folder for your EXE file.")


if __name__ == "__main__":
    install_pyinstaller()
    py_file = find_py_file()
    build_exe(py_file)