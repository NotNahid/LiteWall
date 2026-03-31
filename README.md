# LiteWall - Live Wallpaper Engine for Windows

![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

LiteWall is a production-ready desktop video wallpaper engine for Windows. It allows you to set videos as your desktop background.

## Table of Contents

*   [Features](#features)
*   [Screenshots](#screenshots)
*   [Requirements](#requirements)
*   [How to Run](#how-to-run)
*   [How to Build](#how-to-build)
*   [File Overview](#file-overview)
*   [Contributing](#contributing)
*   [License](#license)

## Features

*   **Video Wallpapers:** Set your favorite videos as your desktop wallpaper.
*   **VLC Backend:** Uses the powerful and reliable VLC media player for video playback. It auto-detects system-wide VLC installations or can auto-download a portable version.
*   **Modern GUI:** A clean and modern graphical user interface built with CustomTkinter. It features a dark theme, a tabbed layout for easy navigation, tooltips for guidance, a history of recently used files, a path entry for direct file access, and sparklines.
*   **Efficient and Responsive:** All VLC and download operations run in separate daemon threads to ensure the UI remains responsive. UI callbacks are safely marshalled back to the main thread.
*   **State Management:** A centralized `_WallpaperState` dataclass manages the application's state, with safe helper methods for reading and writing.
*   **Persistence:** An optional headless mode allows the wallpaper to persist even after the application is closed and across system reboots, using a Windows Registry Run key.

## Screenshots

*(Add screenshots of the application here to showcase the GUI and features.)*

## Requirements

*   **Operating System:** Windows 10 or Windows 11
*   **Python:** Python 3.10 or newer

The following Python packages are required and will be installed automatically when you run the application:

*   `psutil`
*   `python-vlc`
*   `customtkinter`
*   `pystray`
*   `Pillow`
*   `pywin32`

## How to Run

1.  Make sure you have Python 3.10 or newer installed on your Windows system.
2.  Clone this repository:
    ```bash
    git clone https://github.com/NotNahid/litewall.git
    ```
3.  Navigate to the `litewall` directory:
    ```bash
    cd litewall
    ```
4.  Run the `LiteWall.py` script:
    ```bash
    python LiteWall.py
    ```
    The application will start, and the required dependencies will be installed automatically.

## How to Build

If you want to create a standalone executable (`.exe`) file, you can use the provided build script.

1.  Make sure you have Python 3.10 or newer installed.
2.  Navigate to the `litewall` directory.
3.  Run the `build.py` script:
    ```bash
    python build.py
    ```
    This script uses PyInstaller to package the application. The final executable will be located in the `dist` folder.

## File Overview

*   `LiteWall.py`: The main application script containing all the logic for the GUI and wallpaper engine.
*   `build.py`: The build script that uses PyInstaller to create a standalone executable.
*   `notnahid.ico`: The application icon file.
*   `README.md`: This file, providing information about the project.

## Contributing

Contributions are welcome! If you have any ideas, suggestions, or bug reports, please open an issue or create a pull request.

## License

This project is licensed under the MIT License. It is recommended to create a `LICENSE` file with the following content:

```
MIT License

Copyright (c) 2023 NotNahid

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
