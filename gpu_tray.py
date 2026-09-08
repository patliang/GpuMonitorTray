import sys
import time
import threading
import os
import winreg
import subprocess
import json
import pystray
import pynvml
import psutil
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageDraw, ImageFont

APP_NAME = "GpuMemTray"

def set_startup(enable=True):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
        if enable:
            if getattr(sys, 'frozen', False):
                exe_path = f'"{sys.executable}"'
            else:
                exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Startup Registry Error: {e}")

# Initialize NVML Host Drivers
try:
    pynvml.nvmlInit()
    gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    gpu_name = pynvml.nvmlDeviceGetName(gpu_handle)
    if isinstance(gpu_name, bytes):
        gpu_name = gpu_name.decode('utf-8')
except Exception:
    gpu_handle = None
    gpu_name = "NVIDIA GPU Not Found"

running = True
pinned = False
popup_visible = False

def parse_mem_string_to_mb(mem_str):
    """Parses docker stats strings like '18.2GiB / 24GiB' or '350MiB / 24GiB' to MB."""
    try:
        used_part = mem_str.split('/')[0].strip()
        if "GiB" in used_part:
            return int(float(used_part.replace("GiB", "").strip()) * 1024)
        elif "MiB" in used_part:
            return int(float(used_part.replace("MiB", "").strip()))
        elif "KiB" in used_part:
            return int(float(used_part.replace("KiB", "").strip()) / 1024)
        elif "B" in used_part:
            return int(float(used_part.replace("B", "").strip()) / (1024 * 1024))
    except Exception:
        pass
    return 0

def get_real_docker_stats():
    """Queries real per-container RAM/VRAM footprints and CPU usage via Docker CLI."""
    containers = []
    try:
        cmd = ["docker", "stats", "--no-stream", "--format", '{"name":"{{.Name}}","mem":"{{.MemUsage}}","cpu":"{{.CPUPerc}}"}']
        output = subprocess.check_output(cmd, creationflags=subprocess.CREATE_NO_WINDOW, text=True, errors="ignore")
        
        for line in output.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                ram_mb = parse_mem_string_to_mb(data.get("mem", ""))
                
                cpu_raw = data.get("cpu", "0%").replace("%", "").strip()
                cpu_pct = float(cpu_raw) if cpu_raw else 0.0

                containers.append({
                    'pid': '-',
                    'name': f"Docker: {data['name']}",
                    'vram': 0, # Resolved via System driver (PID 4) inside WSL2
                    'ram': ram_mb,
                    'gpu': 0,
                    'cpu': cpu_pct
                })
            except Exception:
                continue
    except Exception:
        pass
    return containers

def get_nvml_process_gpu_util():
    """Reads per-process GPU core usage from host NVML."""
    proc_gpu_util = {}
    if not gpu_handle:
        return proc_gpu_util
    try:
        util_samples = pynvml.nvmlDeviceGetProcessUtilization(gpu_handle, 0)
        for sample in util_samples:
            proc_gpu_util[sample.pid] = sample.smUtil
    except Exception:
        pass
    return proc_gpu_util

def get_active_workloads():
    """Gathers processes, pulls container metrics, and cleans up zero-resource background noise."""
    nvml_gpu_map = get_nvml_process_gpu_util()
    final_list = []
    seen_pids = set()

    # 1. Inspect real Docker Container footprints directly
    docker_containers = get_real_docker_stats()
    if docker_containers:
        final_list.extend(docker_containers)

    # 2. Gather host NVML compute/graphics processes
    nvml_procs = []
    if gpu_handle:
        try:
            nvml_procs.extend(pynvml.nvmlDeviceGetComputeRunningProcesses(gpu_handle))
            nvml_procs.extend(pynvml.nvmlDeviceGetGraphicsRunningProcesses(gpu_handle))
        except Exception:
            pass

    for p in nvml_procs:
        if p.pid in seen_pids:
            continue
        seen_pids.add(p.pid)
        vram_mb = (p.usedGpuMemory // (1024 * 1024)) if p.usedGpuMemory else 0
        gpu_pct = nvml_gpu_map.get(p.pid, 0)
        
        try:
            proc = psutil.Process(p.pid)
            p_name = proc.name()
            ram_mb = proc.memory_info().rss // (1024 * 1024)
            cpu_pct = proc.cpu_percent(interval=None) / psutil.cpu_count()
            
            # Map Python paths running within Stability Matrix
            try:
                cmdline = " ".join(proc.cmdline()).lower()
                if "stabilitymatrix" in cmdline or "comfyui" in cmdline:
                    p_name = "ComfyUI (PyTorch Engine)"
            except Exception:
                pass
        except Exception:
            p_name = f"PID {p.pid}"
            ram_mb = 0
            cpu_pct = 0.0

        if p.pid == 4:
            p_name = "System (WSL2 / DirectX Compute)"

        final_list.append({
            'pid': p.pid, 
            'name': p_name, 
            'vram': vram_mb, 
            'ram': ram_mb,
            'gpu': gpu_pct, 
            'cpu': cpu_pct
        })

    # 3. Inspect target host system processes
    target_keywords = ["chrome.exe", "msedge.exe", "python.exe", "pythonw.exe", "llama-server.exe"]

    for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            p_info = proc.info
            p_name = p_info['name']
            if not p_name:
                continue

            p_name_lower = p_name.lower()
            if any(k in p_name_lower for k in target_keywords):
                pid = p_info['pid']
                if pid in seen_pids:
                    continue

                ram_mb = p_info['memory_info'].rss // (1024 * 1024) if p_info['memory_info'] else 0
                gpu_pct = nvml_gpu_map.get(pid, 0)
                
                try:
                    cpu_pct = proc.cpu_percent(interval=None) / psutil.cpu_count()
                    cmdline = " ".join(proc.cmdline()).lower()
                    if "comfyui" in cmdline or "stabilitymatrix" in cmdline:
                        display_name = "ComfyUI / PyTorch Engine"
                    elif "python" in p_name_lower:
                        display_name = f"Python ({p_name})"
                    else:
                        display_name = p_name
                except Exception:
                    display_name = p_name
                    cpu_pct = 0.0

                seen_pids.add(pid)
                final_list.append({
                    'pid': pid, 
                    'name': display_name, 
                    'vram': 0, 
                    'ram': ram_mb,
                    'gpu': gpu_pct, 
                    'cpu': cpu_pct
                })

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # 4. Consolidate browser sub-processes
    consolidated = []
    chrome_agg = {'pid': '-', 'name': 'Google Chrome', 'vram': 0, 'ram': 0, 'gpu': 0, 'cpu': 0.0, 'count': 0}
    edge_agg = {'pid': '-', 'name': 'Microsoft Edge', 'vram': 0, 'ram': 0, 'gpu': 0, 'cpu': 0.0, 'count': 0}

    for item in final_list:
        n_lower = item['name'].lower()
        if "chrome" in n_lower:
            chrome_agg['vram'] += item['vram']
            chrome_agg['ram'] += item['ram']
            chrome_agg['gpu'] += item['gpu']
            chrome_agg['cpu'] += item['cpu']
            chrome_agg['count'] += 1
        elif "msedge" in n_lower:
            edge_agg['vram'] += item['vram']
            edge_agg['ram'] += item['ram']
            edge_agg['gpu'] += item['gpu']
            edge_agg['cpu'] += item['cpu']
            edge_agg['count'] += 1
        else:
            consolidated.append(item)

    if chrome_agg['count'] > 0:
        chrome_agg['name'] = f"Google Chrome ({chrome_agg['count']} processes)"
        consolidated.append(chrome_agg)

    if edge_agg['count'] > 0:
        edge_agg['name'] = f"Microsoft Edge ({edge_agg['count']} processes)"
        consolidated.append(edge_agg)

    # 5. Filter zero-resource idle processes
    cleaned_processes = [
        p for p in consolidated 
        if p['vram'] > 0 or p['ram'] > 100 or p['gpu'] > 0 or p['cpu'] >= 0.1
    ]

    cleaned_processes.sort(key=lambda x: (x['vram'], x['ram'], x['gpu']), reverse=True)
    return cleaned_processes

def get_gpu_data():
    if not gpu_handle:
        return 0, 0, 0, 0, []
    
    try:
        info = pynvml.nvmlDeviceGetMemoryInfo(gpu_handle)
        used_mb = info.used // (1024 * 1024)
        total_mb = info.total // (1024 * 1024)
        vram_pct = int((info.used / info.total) * 100)
        
        try:
            rates = pynvml.nvmlDeviceGetUtilizationRates(gpu_handle)
            gpu_util = rates.gpu
        except Exception:
            gpu_util = 0

        process_list = get_active_workloads()
        return gpu_util, vram_pct, used_mb, total_mb, process_list

    except Exception:
        return 0, 0, 0, 0, []

def create_tray_icon(percentage):
    width, height = 64, 64
    image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    bg_color = (0, 180, 0, 255) if percentage < 60 else (230, 140, 0, 255) if percentage < 85 else (220, 0, 0, 255)
    draw.rounded_rectangle([2, 2, width - 2, height - 2], radius=10, fill=bg_color)

    text = f"{percentage}%"
    try:
        font = ImageFont.truetype("arial.ttf", 26)
    except IOError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((width - text_w) / 2, (height - text_h) / 2 - bbox[1]), text, fill=(255, 255, 255), font=font)
    return image

class GpuPopupWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("GPU Monitor")
        self.root.geometry("640x450")
        self.root.configure(bg="#1e1e1e")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        
        header_frame = tk.Frame(self.root, bg="#1e1e1e")
        header_frame.pack(fill="x", padx=10, pady=5)

        self.title_label = tk.Label(header_frame, text=gpu_name, font=("Segoe UI", 10, "bold"), fg="#ffffff", bg="#1e1e1e")
        self.title_label.pack(side="left")

        self.pin_btn = tk.Button(header_frame, text="📌 Pin", command=self.toggle_pin, bg="#333333", fg="#ffffff", bd=0, padx=8, pady=2)
        self.pin_btn.pack(side="right")

        self.util_label = tk.Label(self.root, text="GPU Core Util: 0%", font=("Segoe UI", 9, "bold"), fg="#00e676", bg="#1e1e1e")
        self.util_label.pack(anchor="w", padx=10, pady=(5, 0))

        self.vram_label = tk.Label(self.root, text="VRAM: 0 MB / 0 MB (0%)", font=("Segoe UI", 9), fg="#cccccc", bg="#1e1e1e")
        self.vram_label.pack(anchor="w", padx=10, pady=(2, 2))

        self.canvas = tk.Canvas(self.root, height=16, bg="#333333", highlightthickness=0)
        self.canvas.pack(fill="x", padx=10, pady=5)

        tk.Label(self.root, text="Active Workloads & Containers:", font=("Segoe UI", 9, "bold"), fg="#ffffff", bg="#1e1e1e").pack(anchor="w", padx=10, pady=(10, 2))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#2d2d2d", foreground="#ffffff", fieldbackground="#2d2d2d", rowheight=22)
        style.configure("Treeview.Heading", background="#333333", foreground="#ffffff")

        # 6 Columns: PID, Name, VRAM, System RAM, GPU %, CPU %
        self.tree = ttk.Treeview(self.root, columns=("PID", "Name", "VRAM", "RAM", "GPU", "CPU"), show="headings", height=9)
        self.tree.heading("PID", text="PID")
        self.tree.heading("Name", text="Process / Container")
        self.tree.heading("VRAM", text="VRAM")
        self.tree.heading("RAM", text="Sys RAM")
        self.tree.heading("GPU", text="GPU %")
        self.tree.heading("CPU", text="CPU %")

        self.tree.column("PID", width=55, anchor="center")
        self.tree.column("Name", width=250, anchor="w")
        self.tree.column("VRAM", width=80, anchor="e")
        self.tree.column("RAM", width=80, anchor="e")
        self.tree.column("GPU", width=55, anchor="e")
        self.tree.column("CPU", width=55, anchor="e")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.root.withdraw()

    def toggle_pin(self):
        global pinned
        pinned = not pinned
        self.root.attributes("-topmost", pinned)
        self.pin_btn.config(bg="#007acc" if pinned else "#333333")

    def show_window(self):
        global popup_visible
        popup_visible = True
        self.root.deiconify()
        self.root.lift()

    def hide_window(self):
        global popup_visible
        popup_visible = False
        self.root.withdraw()

    def toggle_window(self):
        if popup_visible:
            self.hide_window()
        else:
            self.show_window()

    def update_data(self, gpu_util, vram_pct, used, total, procs):
        self.util_label.config(text=f"GPU Core Util: {gpu_util}%")
        self.vram_label.config(text=f"VRAM: {used:,} MB / {total:,} MB ({vram_pct}%)")
        
        self.canvas.delete("all")
        w = self.canvas.winfo_width()
        if w > 1:
            bar_w = (vram_pct / 100.0) * w
            color = "#00c853" if vram_pct < 60 else "#ff9100" if vram_pct < 85 else "#ff3d00"
            self.canvas.create_rectangle(0, 0, bar_w, 16, fill=color, width=0)

        for item in self.tree.get_children():
            self.tree.delete(item)

        for p in procs:
            vram_str = f"{p['vram']:,} MB" if p['vram'] > 0 else "-"
            ram_str = f"{p['ram']:,} MB" if p['ram'] > 0 else "-"
            gpu_str = f"{p['gpu']}%" if p['gpu'] > 0 else "-"
            cpu_str = f"{p['cpu']:.1f}%"
            self.tree.insert("", "end", values=(p['pid'], p['name'], vram_str, ram_str, gpu_str, cpu_str))

def background_loop(window, icon):
    psutil.cpu_percent(interval=None)
    while running:
        gpu_util, vram_pct, used, total, procs = get_gpu_data()
        
        icon.icon = create_tray_icon(vram_pct)
        icon.title = f"{gpu_name}\nGPU Core: {gpu_util}%\nVRAM: {used:,} MB / {total:,} MB ({vram_pct}%)"

        if popup_visible:
            window.root.after(0, window.update_data, gpu_util, vram_pct, used, total, procs)
            
        time.sleep(2)

def main():
    set_startup(True)
    window = GpuPopupWindow()

    def on_tray_click(icon, item):
        window.root.after(0, window.toggle_window)

    def on_exit(icon, item):
        global running
        running = False
        set_startup(False)
        icon.stop()
        window.root.after(0, window.root.destroy)
        if gpu_handle:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
        sys.exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open / Close Monitor", on_tray_click, default=True),
        pystray.MenuItem("Exit", on_exit)
    )

    icon = pystray.Icon("GpuMemTray", create_tray_icon(0), gpu_name, menu)

    t = threading.Thread(target=background_loop, args=(window, icon), daemon=True)
    t.start()

    threading.Thread(target=icon.run, daemon=True).start()

    window.root.mainloop()

if __name__ == "__main__":
    main()