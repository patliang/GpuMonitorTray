# GpuMonitorTray

A lightweight Windows system tray application designed to monitor real-time GPU compute cores, VRAM usage, system RAM, and active execution processes (including PyTorch engines, ComfyUI, Docker containers, and browser sub-processes).

## Installation & Usage

### Option A: Standalone Executable (Recommended for end-users)
1. Download `GpuMonitorTray.exe` from the latest [GitHub Release](../../releases).
2. Double-click `GpuMonitorTray.exe` to run. No Python setup required.

### Option B: Running from Source
1. **Clone the repository:**
   ```bash
   git clone [https://github.com/YOUR_GITHUB_USERNAME/GpuMonitorTray.git](https://github.com/YOUR_GITHUB_USERNAME/GpuMonitorTray.git)
   cd GpuMonitorTray

```

2. **Set up Virtual Environment:**
```bash
python -m venv .venv
.venv\Scripts\activate

```


3. **Install Dependencies & Run:**
```bash
pip install -r requirements.txt
python gpu_tray.py

```



## How to Build `.exe`

To build the executable manually using PyInstaller:

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name GpuMonitorTray gpu_tray.py

```

The output binary will be generated in `dist/GpuMonitorTray.exe`.

## License

This project is licensed under the [MIT License](https://www.google.com/search?q=LICENSE).

``` 