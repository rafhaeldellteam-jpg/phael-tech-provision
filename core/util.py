"""Utilitários compartilhados: caminhos, PowerShell, processos, admin e log."""
from __future__ import annotations

import ctypes
import datetime as _dt
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable

APP_NAME = "Phael Tech Provision"
from .version import VERSION as APP_VERSION, GITHUB_REPO  # noqa: E402
IS_WIN = os.name == "nt"

CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
CREATE_NEW_CONSOLE = 0x00000010 if IS_WIN else 0


# --------------------------------------------------------------------------- caminhos
def app_dir() -> Path:
    """Pasta onde está o .exe (ou o app.py ao rodar pelo código-fonte)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path:
    """Pasta interna do PyInstaller (_MEIPASS) ou a pasta do projeto."""
    return Path(getattr(sys, "_MEIPASS", app_dir()))


def resource_roots() -> list[Path]:
    """Ordem de busca dos recursos: pasta 'Recursos' ao lado do exe tem prioridade,
    assim dá para atualizar instaladores sem recompilar o programa."""
    roots = [app_dir() / "Recursos", app_dir(), bundle_dir() / "Recursos", bundle_dir()]
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def find_resource(*names: str) -> Path | None:
    for root in resource_roots():
        for name in names:
            p = root / name
            if p.exists():
                return p
    return None


def work_dir() -> Path:
    """Pasta persistente (sobrevive a reinicializações) para estado, logs e downloads."""
    base = os.environ.get("ProgramData") if IS_WIN else tempfile.gettempdir()
    d = Path(base or tempfile.gettempdir()) / "PhaelTechProvision"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sub_work_dir(name: str) -> Path:
    d = work_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- admin
def is_admin() -> bool:
    if not IS_WIN:
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(extra_args: list[str] | None = None) -> bool:
    if not IS_WIN:
        return False
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, list(sys.argv[1:])
    else:
        exe, args = sys.executable, [str(Path(sys.argv[0]).resolve())] + list(sys.argv[1:])
    args += extra_args or []
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(args), str(app_dir()), 1)
    return int(rc) > 32


# --------------------------------------------------------------------------- log
class Logger:
    """Log em arquivo (ProgramData\\PhaelTechProvision\\logs) + callback para a interface."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sinks: list[Callable[[str, str, str], None]] = []
        self.path = sub_work_dir("logs") / f"provision-{_dt.date.today():%Y%m%d}.log"

    def add_sink(self, fn: Callable[[str, str, str], None]) -> None:
        self._sinks.append(fn)

    def __call__(self, msg: str, level: str = "info") -> None:
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] [{level.upper()}] {msg}"
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        for sink in list(self._sinks):
            try:
                sink(ts, level, msg)
            except Exception:
                pass


log = Logger()


# --------------------------------------------------------------------------- processos
def _decode(b: bytes) -> str:
    for enc in ("utf-8", "cp850", "cp1252"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


PS_PREAMBLE = (
    "$ProgressPreference = 'SilentlyContinue'\n"
    "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\n"
    "try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch {}\n"
)


def _write_ps(script: str) -> Path:
    # UTF-8 com BOM: o Windows PowerShell 5.1 só lê acentos corretamente assim.
    p = sub_work_dir("scripts") / f"ps-{uuid.uuid4().hex[:10]}.ps1"
    p.write_text(PS_PREAMBLE + script, encoding="utf-8-sig")
    return p


def _ps_cmd(path: Path) -> list[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)]


def run_ps(script: str, timeout: int | None = 120) -> subprocess.CompletedProcess:
    """Executa um script PowerShell (via arquivo .ps1, sem limite de tamanho/aspas)."""
    if not IS_WIN:
        return subprocess.CompletedProcess([], 1, "", "PowerShell indisponível fora do Windows")
    path = _write_ps(script)
    try:
        r = subprocess.run(_ps_cmd(path), capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return subprocess.CompletedProcess(r.args, r.returncode, _decode(r.stdout), _decode(r.stderr))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], -1, "", "Tempo limite esgotado")
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def stream_ps(script: str, on_line: Callable[[str], None], timeout: int | None = None,
              cancel: threading.Event | None = None) -> int:
    """Executa PowerShell entregando cada linha de saída ao callback (para progresso em tempo real)."""
    if not IS_WIN:
        on_line("PowerShell indisponível fora do Windows")
        return 1
    path = _write_ps(script)
    try:
        return stream_cmd(_ps_cmd(path), on_line, timeout=timeout, cancel=cancel)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def stream_cmd(cmd: list[str], on_line: Callable[[str], None], timeout: int | None = None,
               cancel: threading.Event | None = None, cwd: str | None = None) -> int:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW, cwd=cwd)
    timer = None
    if timeout:
        timer = threading.Timer(timeout, lambda: _kill(proc))
        timer.daemon = True
        timer.start()
    watcher = None
    if cancel is not None:
        def _watch():
            while proc.poll() is None:
                if cancel.wait(0.5):
                    _kill(proc)
                    return
        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        line = _decode(raw).rstrip("\r\n")
        if line.strip():
            on_line(line)
    rc = proc.wait()
    if timer:
        timer.cancel()
    return rc


def _kill(proc: subprocess.Popen) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        else:
            proc.kill()
    except Exception:
        pass


def run(cmd: list[str], timeout: int | None = 120, cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd, creationflags=CREATE_NO_WINDOW)
        return subprocess.CompletedProcess(r.args, r.returncode, _decode(r.stdout), _decode(r.stderr))
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 9009, "", str(e))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, -1, "", "Tempo limite esgotado")


def unblock(path: Path) -> None:
    """Remove a marca 'baixado da internet' (Zone.Identifier) para evitar o SmartScreen."""
    if IS_WIN:
        try:
            ctypes.windll.kernel32.DeleteFileW(str(path) + ":Zone.Identifier")
        except Exception:
            pass


def task_names() -> set[str]:
    if not IS_WIN:
        return set()
    r = run(["tasklist", "/FO", "CSV", "/NH"], timeout=30)
    names = set()
    for line in r.stdout.splitlines():
        if line.startswith('"'):
            names.add(line.split('","')[0].strip('"').lower())
    return names


def ps_quote(s: str) -> str:
    """Aspas simples seguras para PowerShell."""
    return "'" + str(s).replace("'", "''") + "'"


def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
