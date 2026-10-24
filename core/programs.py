"""Catálogo de aplicativos (instaladores embutidos na pasta Recursos\\Programas)."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .provision import Progress, install_office, office_installed
from .util import find_resource, log, run, task_names, unblock
from .drivers import winget_install

PF = os.environ.get("ProgramFiles", r"C:\Program Files")
PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
LAD = os.environ.get("LOCALAPPDATA", "")

PROGRAMS: list[dict] = [
    {"key": "office", "title": "Microsoft Office", "desc": "Microsoft 365 — instalador embutido (requer internet).",
     "special": "office", "icon": "office", "default": True},
    {"key": "chrome", "title": "Google Chrome", "desc": "Navegador — instalação silenciosa.",
     "file": "ChromeSetup.exe", "args": ["/silent", "/install"], "icon": "chrome", "default": True,
     "detect": [rf"{PF}\Google\Chrome\Application\chrome.exe", rf"{PF86}\Google\Chrome\Application\chrome.exe"],
     "watch": ("chromesetup", "googleupdate", "chrome_installer", "updater.exe")},
    {"key": "anydesk", "title": "AnyDesk", "desc": "Acesso remoto — instalação silenciosa.",
     "file": "AnyDesk.exe", "args": ["--install", rf"{PF86}\AnyDesk", "--start-with-win", "--create-shortcuts",
                                    "--create-desktop-icon", "--silent"], "icon": "anydesk", "default": True,
     "detect": [rf"{PF86}\AnyDesk\AnyDesk.exe", rf"{PF}\AnyDesk\AnyDesk.exe"]},
    {"key": "winrar", "title": "WinRAR", "desc": "Compactador de arquivos — instalação silenciosa.",
     "file": "winrar-x64-723.exe", "args": ["/S"], "icon": "winrar", "default": True,
     "detect": [rf"{PF}\WinRAR\WinRAR.exe"]},
    {"key": "firefox", "title": "Mozilla Firefox", "desc": "Navegador alternativo.",
     "file": "Firefox Installer.exe", "args": ["/S"], "icon": "firefox", "default": False,
     "detect": [rf"{PF}\Mozilla Firefox\firefox.exe"], "watch": ("firefox installer", "setup-stub", "setup.exe")},
    {"key": "brave", "title": "Brave Browser", "desc": "Navegador com bloqueio de anúncios.",
     "file": "BraveBrowserSetup-BRV011.exe", "args": ["/silent", "/install"], "icon": "brave", "default": False,
     "detect": [rf"{PF}\BraveSoftware\Brave-Browser\Application\brave.exe"],
     "watch": ("bravebrowsersetup", "brave_installer", "braveupdate")},
    {"key": "crystaldiskinfo", "title": "CrystalDiskInfo", "desc": "Saúde dos discos (SMART).",
     "file": "CrystalDiskInfo9_9_3.exe", "args": ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
     "icon": "disk", "default": False, "detect": [rf"{PF}\CrystalDiskInfo\DiskInfo64.exe"]},
    {"key": "cpuz", "title": "CPU-Z", "desc": "Informações detalhadas de processador e memória.",
     "file": "cpu-z_3.01-en.exe", "args": ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], "icon": "cpu",
     "default": False, "detect": [rf"{PF}\CPUID\CPU-Z\cpuz.exe"]},
    {"key": "ddu", "title": "Display Driver Uninstaller", "desc": "Remove drivers de vídeo — abre a janela do DDU.",
     "file": "DDU v18.1.6.1.exe", "args": [], "icon": "gpu", "default": False, "visible": True},
    {"key": "powertoys", "title": "Microsoft PowerToys", "desc": "Ferramentas extras da Microsoft (via winget).",
     "winget": "Microsoft.PowerToys", "icon": "toys", "default": False, "note": "Requer internet"},
]


def program_file(entry: dict) -> Path | None:
    if not entry.get("file"):
        return None
    return find_resource("Programas/" + entry["file"], entry["file"])


def is_available(entry: dict) -> bool:
    return entry.get("special") == "office" or bool(entry.get("winget")) or program_file(entry) is not None


def is_installed(entry: dict) -> bool:
    if entry.get("special") == "office":
        return bool(office_installed())
    return any(Path(p).exists() for p in entry.get("detect", []))


def install_program(entry: dict, progress: Progress, cancel: threading.Event) -> str:
    title = entry["title"]
    if entry.get("special") == "office":
        return install_office(progress, cancel, force=False)
    if entry.get("winget"):
        progress(-1, "Instalando via winget…")
        if not winget_install(entry["winget"], cancel):
            raise RuntimeError("winget falhou (verifique a internet).")
        return "Instalado."
    if is_installed(entry):
        log(f"{title} já está instalado.", "ok")
        progress(100, "Já instalado")
        return "Já instalado."
    f = program_file(entry)
    if not f:
        raise FileNotFoundError(f"{entry['file']} não encontrado em Recursos\\Programas.")
    unblock(f)
    progress(-1, f"Instalando {title}…")
    log(f"Iniciando {f.name} {' '.join(entry.get('args', []))}".rstrip())
    proc = subprocess.Popen([str(f)] + entry.get("args", []), cwd=str(f.parent))
    start = time.time()
    limit = 1800 if not entry.get("visible") else 7200
    while proc.poll() is None:
        if cancel.wait(1):
            raise RuntimeError("Cancelado")
        if time.time() - start > limit:
            raise RuntimeError("Instalador demorou demais.")
    # bootstrappers (Chrome/Firefox/Brave) fecham antes de terminar: espera os processos filhos
    watch = entry.get("watch")
    if watch:
        t1 = time.time()
        while time.time() - t1 < 900:
            names = task_names()
            if not any(w in n for n in names for w in watch):
                break
            if cancel.wait(2):
                raise RuntimeError("Cancelado")
    rc = proc.returncode
    if entry.get("detect"):
        for _ in range(10):
            if is_installed(entry):
                break
            time.sleep(1)
        if not is_installed(entry):
            raise RuntimeError(f"Instalador terminou (código {rc}), mas {title} não foi encontrado.")
    elif rc not in (0, 3010, 1641) and not entry.get("visible"):
        raise RuntimeError(f"Instalador retornou código {rc}.")
    progress(100, "Instalado")
    log(f"{title}: instalado.", "ok")
    return "Instalado."
