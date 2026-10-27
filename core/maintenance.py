"""Rede, limpeza, reparo do Windows e sensores de temperatura."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .util import (CREATE_NO_WINDOW, IS_WIN, find_resource, fmt_bytes, log, ps_quote, run, run_ps, stream_cmd,
                   sub_work_dir)

# =========================================================================== rede
NETWORK_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($a in (Get-NetAdapter -Physical | Sort-Object Status, ifIndex)) {
    $ip = Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '169.254*' } | Select-Object -First 1
    $cfg = Get-NetIPConfiguration -InterfaceIndex $a.ifIndex
    $wifi = ($a.Name -match 'Wi-?Fi|Wireless|WLAN' -or $a.InterfaceDescription -match 'Wi-?Fi|Wireless|802\.11|WLAN')
    $ssid = ''
    if ($wifi -and $a.Status -eq 'Up') {
        $m = (netsh wlan show interfaces) -match '^\s*SSID\s*:' | Select-Object -First 1
        if ($m) { $ssid = ($m -split ':', 2)[1].Trim() }
    }
    $out += [pscustomobject]@{
        name = $a.Name; desc = $a.InterfaceDescription; status = [string]$a.Status
        kind = if ($wifi) { 'Wi-Fi' } else { 'Ethernet' }
        mac = $a.MacAddress; speed = [string]$a.LinkSpeed
        ip = if ($ip) { $ip.IPAddress + '/' + $ip.PrefixLength } else { '' }
        gateway = if ($cfg.IPv4DefaultGateway) { $cfg.IPv4DefaultGateway.NextHop } else { '' }
        dns = (@($cfg.DNSServer | Where-Object { $_.AddressFamily -eq 2 } | ForEach-Object { $_.ServerAddresses }) -join ', ')
        dhcp = [string](Get-NetIPInterface -InterfaceIndex $a.ifIndex -AddressFamily IPv4).Dhcp
        ssid = $ssid
    }
}
$prof = Get-NetConnectionProfile | Select-Object -First 1
[pscustomobject]@{ adapters = $out; profile = if ($prof) { $prof.Name } else { '' }; internet = if ($prof) { [string]$prof.IPv4Connectivity } else { 'Disconnected' } } | ConvertTo-Json -Compress -Depth 4
"""


def network_info() -> dict:
    r = run_ps(NETWORK_PS, timeout=60)
    s = r.stdout.strip()
    if not s:
        raise RuntimeError(r.stderr.strip() or "Falha ao consultar a rede.")
    data = json.loads(s[s.find("{"):])
    ad = data.get("adapters") or []
    data["adapters"] = ad if isinstance(ad, list) else [ad]
    return data


def connectivity_test(on_line: Callable[[str], None], gateway: str = "") -> dict:
    """Testa gateway, internet (IP) e DNS — diagnóstico real, não só o loopback."""
    targets = []
    if gateway:
        targets.append(("Gateway (roteador)", gateway))
    targets += [("Internet (8.8.8.8)", "8.8.8.8"), ("DNS (google.com)", "google.com"),
                ("Windows Update", "download.windowsupdate.com")]
    results = {}
    for label, host in targets:
        on_line(f"Testando {label} — {host}…")
        r = run(["ping", "-n", "3", "-w", "1500", host], timeout=20)
        out = r.stdout
        ok = "TTL=" in out.upper()
        avg = ""
        for ln in out.splitlines():
            low = ln.lower()
            if "média" in low or "average" in low or "media" in low:
                avg = ln.split("=")[-1].strip()
        results[label] = {"ok": ok, "avg": avg}
        on_line(f"  {'✔' if ok else '✘'} {label}: {'respondeu' if ok else 'sem resposta'}{' — média ' + avg if avg else ''}")
    return results


# =========================================================================== limpeza
def free_space(drive: str = "C:\\") -> int:
    try:
        return shutil.disk_usage(drive).free
    except OSError:
        return 0


def _clean(path: Path) -> tuple[int, int]:
    from .provision import _protected
    freed = files = 0
    if not path.is_dir() or _protected(path):
        return 0, 0
    for item in list(path.iterdir()):
        if _protected(item):
            continue
        try:
            if item.is_dir() and not item.is_symlink():
                f, n = _clean(item)
                freed += f
                files += n
                try:
                    item.rmdir()
                except OSError:
                    pass
            else:
                size = item.stat().st_size
                item.unlink()
                freed += size
                files += 1
        except OSError:
            continue
    return freed, files


def clean_temp(on_line: Callable[[str], None]) -> int:
    paths = [Path(os.environ.get("TEMP", "")), Path(r"C:\Windows\Temp"),
             Path(r"C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache")]
    users = Path(r"C:\Users")
    if users.is_dir():
        for u in users.iterdir():
            for sub in (r"AppData\Local\Temp", r"AppData\Local\Microsoft\Windows\INetCache",
                        r"AppData\Local\CrashDumps"):
                p = u / sub
                if p.is_dir() and p not in paths:
                    paths.append(p)
    total = 0
    for p in paths:
        f, n = _clean(p)
        total += f
        if n:
            on_line(f"{p}: {n} arquivo(s), {fmt_bytes(f)}")
    on_line(f"Temporários: {fmt_bytes(total)} liberados.")
    return total


def clean_windows_update(on_line: Callable[[str], None]) -> int:
    on_line("Parando serviços do Windows Update…")
    run_ps("Stop-Service wuauserv,bits,dosvc -Force -ErrorAction SilentlyContinue", timeout=90)
    f, n = _clean(Path(r"C:\Windows\SoftwareDistribution\Download"))
    run_ps("Start-Service wuauserv,bits -ErrorAction SilentlyContinue", timeout=90)
    on_line(f"Cache do Windows Update: {n} arquivo(s), {fmt_bytes(f)} liberados. Serviços reativados.")
    return f


def clean_component_store(on_line: Callable[[str], None], cancel: threading.Event | None = None) -> int:
    before = free_space()
    on_line("DISM /StartComponentCleanup (remove versões antigas de atualizações — pode demorar)…")
    stream_cmd(["dism", "/Online", "/Cleanup-Image", "/StartComponentCleanup"], lambda l: _dism_line(l, on_line),
               timeout=3600, cancel=cancel)
    freed = max(0, free_space() - before)
    on_line(f"Limpeza de componentes concluída ({fmt_bytes(freed)}).")
    return freed


def empty_recycle_bin(on_line: Callable[[str], None]) -> int:
    before = free_space()
    run_ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue", timeout=120)
    freed = max(0, free_space() - before)
    on_line(f"Lixeira esvaziada ({fmt_bytes(freed)}).")
    return freed


def startup_programs() -> list[dict]:
    r = run_ps("Get-CimInstance Win32_StartupCommand | Select-Object Name, Command, Location, User | "
               "ConvertTo-Json -Compress", timeout=60)
    s = r.stdout.strip()
    if not s:
        return []
    data = json.loads(s)
    return data if isinstance(data, list) else [data]


def analyze_space(on_line: Callable[[str], None]) -> list[dict]:
    up = os.environ.get("USERPROFILE", r"C:\Users\Public")
    targets = [("Windows", r"C:\Windows"), ("Program Files", r"C:\Program Files"),
               ("Program Files (x86)", r"C:\Program Files (x86)"), ("ProgramData", r"C:\ProgramData"),
               ("Usuários", r"C:\Users"), ("Downloads", os.path.join(up, "Downloads")),
               ("Windows Update (cache)", r"C:\Windows\SoftwareDistribution\Download"),
               ("Windows.old", r"C:\Windows.old")]
    out = []
    for label, p in targets:
        if not os.path.isdir(p):
            continue
        on_line(f"Medindo {label}…")
        total = 0
        for root, _dirs, files in os.walk(p, onerror=lambda e: None):
            for fn in files:
                try:
                    total += os.lstat(os.path.join(root, fn)).st_size
                except OSError:
                    pass
        out.append({"label": label, "path": p, "size": total})
    out.sort(key=lambda x: x["size"], reverse=True)
    for o in out:
        on_line(f"{o['label']:<26}{fmt_bytes(o['size']):>12}")
    return out


# =========================================================================== reparo
REPAIR_STEPS = [
    {"key": "check", "title": "DISM /CheckHealth", "desc": "Verificação rápida de corrupção",
     "cmd": ["dism", "/Online", "/Cleanup-Image", "/CheckHealth"]},
    {"key": "scan", "title": "DISM /ScanHealth", "desc": "Verificação completa da imagem",
     "cmd": ["dism", "/Online", "/Cleanup-Image", "/ScanHealth"]},
    {"key": "restore", "title": "DISM /RestoreHealth", "desc": "Repara a imagem usando o Windows Update",
     "cmd": ["dism", "/Online", "/Cleanup-Image", "/RestoreHealth"]},
    {"key": "sfc", "title": "SFC /scannow", "desc": "Verifica e repara arquivos do sistema",
     "cmd": ["sfc", "/scannow"]},
]


def _dism_line(line: str, on_line: Callable[[str], None], on_pct: Callable[[float], None] | None = None) -> None:
    s = line.replace("\x00", "").strip()
    if not s:
        return
    # barra do DISM: [=====   45.2%   ]  -> vira porcentagem
    if "%" in s and "[" in s:
        try:
            pct = float(s.split("%")[0].split()[-1].replace(",", "."))
            if on_pct:
                on_pct(pct)
            return
        except ValueError:
            pass
    on_line(s)


def run_repair_step(step: dict, on_line: Callable[[str], None], on_pct: Callable[[float], None],
                    cancel: threading.Event) -> int:
    cmd = step["cmd"]
    if cmd[0] == "sfc":
        # SFC escreve em UTF-16; roda pelo cmd para normalizar a saída
        cmd = ["cmd", "/c", "chcp 65001 >nul & sfc /scannow"]
    return stream_cmd(cmd, lambda l: _dism_line(l, on_line, on_pct), timeout=4 * 3600, cancel=cancel)


# =========================================================================== sensores (LibreHardwareMonitor)
SENSORS_PS = r"""
$ErrorActionPreference = 'Continue'
try { [System.Threading.Thread]::CurrentThread.CurrentCulture = 'en-US' } catch {}
Add-Type -Path (Join-Path __DLL_DIR__ 'LibreHardwareMonitorLib.dll')
try { Add-Type -Path (Join-Path __DLL_DIR__ 'HidSharp.dll') } catch {}
$comp = New-Object LibreHardwareMonitor.Hardware.Computer
$comp.IsCpuEnabled = $true; $comp.IsGpuEnabled = $true; $comp.IsMemoryEnabled = $true
$comp.IsMotherboardEnabled = $true; $comp.IsStorageEnabled = $true; $comp.IsBatteryEnabled = $true
$comp.Open()
$outFile = __OUT_FILE__
$dono = __PARENT_PID__
while ($true) {
    # o programa fechou (ou caiu): encerra junto, em vez de ficar rodando escondido
    if (-not (Get-Process -Id $dono -ErrorAction SilentlyContinue)) { break }
    $cpuT = @(); $cpuL = $null; $gpuT = @(); $gpuL = $null; $memL = $null; $boardT = @()
    $disks = New-Object System.Collections.ArrayList; $fans = New-Object System.Collections.ArrayList
    $cpuP = $null; $diskMax = @{}; $fanSeen = @{}
    $stack = New-Object System.Collections.Stack
    foreach ($h in $comp.Hardware) { $stack.Push($h) }
    while ($stack.Count -gt 0) {
        $hw = $stack.Pop(); $type = [string]$hw.HardwareType
        try { $hw.Update() } catch {}
        foreach ($s in $hw.Sensors) {
            if ($null -eq $s.Value) { continue }
            $st = [string]$s.SensorType; $v = [double]$s.Value
            if ($st -eq 'Temperature') {
                if ($type -match 'Cpu') { if ($s.Name -notmatch 'Distance') { $cpuT += $v } }
                elseif ($type -match 'Gpu') { $gpuT += $v }
                elseif ($type -match 'Storage|Hdd|Ssd') {
                    # um SSD NVMe expõe vários sensores (Composite, Temperature 1, 2...): guarda só o maior por disco
                    $key = [string]$hw.Identifier
                    if (-not $diskMax.ContainsKey($key) -or $v -gt $diskMax[$key].t) { $diskMax[$key] = @{ n = $hw.Name; t = [math]::Round($v, 0) } }
                }
                elseif ($type -match 'Motherboard|SuperIO|EmbeddedController') { $boardT += $v }
            } elseif ($st -eq 'Load') {
                if ($type -match 'Cpu' -and $s.Name -eq 'CPU Total') { $cpuL = [math]::Round($v, 0) }
                elseif ($type -match 'Gpu' -and $s.Name -match 'Core|D3D 3D') { if ($null -eq $gpuL -or $v -gt $gpuL) { $gpuL = [math]::Round($v, 0) } }
                elseif ($type -match 'Memory' -and $s.Name -eq 'Memory') { $memL = [math]::Round($v, 0) }
            } elseif ($st -eq 'Power' -and $type -match 'Cpu' -and $s.Name -match 'Package') { $cpuP = [math]::Round($v, 1) }
            elseif ($st -eq 'Fan' -and $v -gt 0) {
                $fk = [string]$hw.Identifier + '|' + $s.Name
                if (-not $fanSeen.ContainsKey($fk)) { $fanSeen[$fk] = $true; [void]$fans.Add(@{ n = $s.Name; r = [math]::Round($v, 0) }) }
            }
        }
        foreach ($sub in $hw.SubHardware) { $stack.Push($sub) }
    }
    foreach ($d in $diskMax.Values) { [void]$disks.Add($d) }
    $cpuTemp = if ($cpuT.Count) { [math]::Round(($cpuT | Measure-Object -Maximum).Maximum, 0) } else { $null }
    $gpuTemp = if ($gpuT.Count) { [math]::Round(($gpuT | Measure-Object -Maximum).Maximum, 0) } else { $null }
    $boardTemp = if ($boardT.Count) { [math]::Round(($boardT | Measure-Object -Maximum).Maximum, 0) } else { $null }
    # alternativas quando o LHM não acessa o sensor (fallbacks APLICADOS antes de montar a saída)
    if ($disks.Count -eq 0) {
        try { Get-PhysicalDisk | Sort-Object DeviceId -Unique | ForEach-Object { $rc = $_ | Get-StorageReliabilityCounter; if ($rc.Temperature -gt 0) { [void]$disks.Add(@{ n = $_.FriendlyName; t = [int]$rc.Temperature }) } } } catch {}
    }
    if ($null -eq $cpuTemp) {
        try {
            $tz = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop | Where-Object { $_.CurrentTemperature -gt 0 } | Select-Object -First 1
            if ($tz) { $t = [math]::Round(($tz.CurrentTemperature / 10.0) - 273.15, 0); if ($t -gt 5 -and $t -lt 120) { $cpuTemp = $t } }
        } catch {}
    }
    $out = [ordered]@{ cpu_t = $cpuTemp; cpu_l = $cpuL; cpu_p = $cpuP; gpu_t = $gpuTemp; gpu_l = $gpuL; mem_l = $memL; board_t = $boardTemp; disks = @($disks); fans = @($fans); ts = (Get-Date).ToString('HH:mm:ss') }
    [System.IO.File]::WriteAllText($outFile, ($out | ConvertTo-Json -Compress -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
    Start-Sleep -Milliseconds 2000
}
"""


class SensorReader:
    """Mantém um PowerShell em segundo plano lendo os sensores a cada 2 s."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.out = sub_work_dir("sensores") / "sensores.json"
        self.error = ""

    def start(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        dll = find_resource("LibreHardwareMonitorLib.dll", "Sensores/LibreHardwareMonitorLib.dll")
        if not dll or not IS_WIN:
            self.error = "LibreHardwareMonitorLib.dll não encontrada — sensores indisponíveis."
            return False
        script = SENSORS_PS.replace("__DLL_DIR__", ps_quote(str(dll.parent))).replace("__OUT_FILE__", ps_quote(str(self.out))).replace("__PARENT_PID__", str(os.getpid()))
        ps1 = sub_work_dir("sensores") / "sensores.ps1"
        ps1.write_text(script, encoding="utf-8-sig")
        self.proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=CREATE_NO_WINDOW)
        return True

    def read(self) -> dict | None:
        if self.proc and self.proc.poll() is not None:
            self.error = "Leitura de sensores encerrada."
            return None
        try:
            data = json.loads(self.out.read_text(encoding="utf-8"))
        except Exception:
            return None
        # garantia extra contra duplicados: um item por disco/ventoinha (maior leitura)
        for key, val in (("disks", "t"), ("fans", "r")):
            items = data.get(key) or []
            if isinstance(items, dict):
                items = [items]
            best: dict = {}
            for it in items:
                n = str(it.get("n", "")).strip()
                if n and (n not in best or (it.get(val) or 0) > (best[n].get(val) or 0)):
                    best[n] = it
            data[key] = list(best.values())
        return data

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        self.proc = None
