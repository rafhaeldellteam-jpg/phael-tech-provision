"""Drivers do fabricante, detectados automaticamente pela marca da máquina.

Dell     -> Dell Command | Update (dcu-cli)       — instala via pasta Recursos ou winget
Lenovo   -> LSUClient (catálogo oficial Lenovo)   — módulo embutido, sem instalar nada
HP       -> HP Image Assistant (HPIA)             — usa Recursos\\HPIA ou baixa do site da HP
Positivo -> Windows Update (catálogo de drivers da Microsoft) + página de suporte
Outros   -> Windows Update (drivers)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .provision import NeedsReboot, Progress, windows_update_round
from .util import (CREATE_NO_WINDOW, IS_WIN, find_resource, log, ps_quote, run, run_ps, stream_cmd, stream_ps,
                   sub_work_dir, unblock)

# --------------------------------------------------------------------------- winget
WINGET_PS = r"""
$w = Get-Command winget.exe -ErrorAction SilentlyContinue
if (-not $w) {
    try { Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction Stop } catch {}
    $cand = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path $cand) { Write-Output $cand; exit 0 }
    $pkg = Get-ChildItem 'C:\Program Files\WindowsApps' -Filter 'Microsoft.DesktopAppInstaller_*_x64__8wekyb3d8bbwe' -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($pkg -and (Test-Path (Join-Path $pkg.FullName 'winget.exe'))) { Write-Output (Join-Path $pkg.FullName 'winget.exe'); exit 0 }
    exit 1
}
Write-Output $w.Source
"""


def winget_path() -> str | None:
    r = run_ps(WINGET_PS, timeout=90)
    p = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    return p if p and Path(p).exists() else None


def winget_install(pkg_id: str, cancel: threading.Event | None = None) -> bool:
    wg = winget_path()
    if not wg:
        log("winget não está disponível nesta máquina.", "warn")
        return False
    log(f"winget install {pkg_id} (requer internet)…")
    rc = stream_cmd([wg, "install", "--id", pkg_id, "-e", "--silent", "--accept-package-agreements",
                     "--accept-source-agreements", "--disable-interactivity", "--source", "winget"],
                    lambda ln: None if set(ln.strip()) <= set("-\\|/ █▒") else log("  " + ln.strip()),
                    timeout=1800, cancel=cancel)
    # -1978335189 = já instalado / sem atualização aplicável
    return rc in (0, -1978335189, 2316632107)


def _wait_proc(proc: subprocess.Popen, progress: Progress, label: str, cancel: threading.Event,
               limit: int = 3 * 3600) -> int:
    start = time.time()
    while proc.poll() is None:
        if cancel.wait(3):
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
            raise RuntimeError("Cancelado")
        el = time.time() - start
        if el > limit:
            proc.kill()
            raise RuntimeError(f"{label}: tempo limite esgotado.")
        progress(min(95.0, 10 + 85 * (1 - pow(2.71828, -el / 900))), f"{label}… {int(el // 60)} min")
    return proc.returncode


# =========================================================================== Dell
DCU_PATHS = [r"C:\Program Files\Dell\CommandUpdate\dcu-cli.exe",
             r"C:\Program Files (x86)\Dell\CommandUpdate\dcu-cli.exe"]
DCU_CODES = {0: "Atualizações aplicadas", 1: "Reinicialização necessária", 5: "Reinicialização necessária",
             500: "Nenhuma atualização disponível", 2: "Erro desconhecido", 3: "Erro ao aplicar",
             4: "Sistema não suportado pelo DCU", 7: "Precisa de privilégio de administrador",
             8: "Plataforma não suportada", 1000: "Erro ao obter o resultado", 1001: "Cancelado",
             3000: "Serviço do DCU não está rodando", 3005: "Serviço do DCU desativado"}


def _dcu_cli() -> str | None:
    for p in DCU_PATHS:
        if Path(p).is_file():
            return p
    return None


def _install_dcu(progress: Progress, cancel: threading.Event) -> str | None:
    # 1) instalador colocado em Recursos (ex.: Dell-Command-Update-Windows-Universal-Application_*.EXE)
    for folder in ("Drivers Dell", "Dell", "Programas"):
        d = find_resource(folder)
        if d and d.is_dir():
            for f in sorted(d.iterdir()):
                if f.suffix.lower() == ".exe" and "command" in f.name.lower() and "update" in f.name.lower():
                    progress(-1, f"Instalando {f.name}…")
                    log(f"Instalando Dell Command | Update a partir de {f.name}.")
                    unblock(f)
                    run([str(f), "/s"], timeout=900)
                    time.sleep(5)
                    if _dcu_cli():
                        return _dcu_cli()
    # 2) winget
    progress(-1, "Instalando Dell Command | Update via winget…")
    for pkg in ("Dell.CommandUpdate.Universal", "Dell.CommandUpdate"):
        if winget_install(pkg, cancel) and _dcu_cli():
            return _dcu_cli()
    return None


def dell_update(progress: Progress, cancel: threading.Event, include_bios: bool = False) -> str:
    cli = _dcu_cli() or _install_dcu(progress, cancel)
    if not cli:
        raise RuntimeError("Dell Command | Update não encontrado e não foi possível instalar. Coloque o instalador "
                           "do DCU na pasta Recursos\\Drivers Dell ou conecte a internet.")
    logs = sub_work_dir("logs")
    run([cli, "/configure", "-userConsent=disable", "-scheduleManual", "-autoSuspendBitLocker=enable"], timeout=120)
    types = "driver,application,others" + (",bios,firmware" if include_bios else "")
    progress(5, "Dell Command | Update: procurando drivers para este modelo…")
    log(f"dcu-cli /applyUpdates (tipos: {types})")

    def on_line(ln: str):
        s = ln.strip()
        if not s:
            return
        if "%" in s and ("Download" in s or "Install" in s):
            progress(-1, s[:120])
        else:
            log("  DCU: " + s)

    rc = stream_cmd([cli, "/applyUpdates", "-silent", "-reboot=disable", f"-updateType={types}",
                     f"-outputLog={logs / 'dell-dcu.log'}"], on_line, timeout=3 * 3600, cancel=cancel)
    msg = DCU_CODES.get(rc, f"código {rc}")
    if rc in (0, 500):
        progress(100, msg)
        log(f"Dell: {msg}.", "ok")
        return msg
    if rc in (1, 5):
        progress(100, msg)
        log(f"Dell: drivers instalados — {msg.lower()}.", "ok")
        raise NeedsReboot("Drivers Dell instalados; reinicialização necessária.")
    raise RuntimeError(f"Dell Command | Update: {msg}.")


# =========================================================================== Lenovo
LENOVO_PS = r"""
$ErrorActionPreference = 'Stop'
function P($pct, $t) { Write-Output ("P|{0}|{1}" -f [int]$pct, $t) }
function L($t) { Write-Output ("L|" + $t) }
try {
    Import-Module (Join-Path __MOD__ 'LSUClient.psd1') -Force
    $dl = __DL__
    New-Item -ItemType Directory -Path $dl -Force | Out-Null
    P 3 'Consultando o catalogo da Lenovo para este modelo...'
    $all = @(Get-LSUpdate -WarningAction SilentlyContinue)
    $todo = @($all | Where-Object { $_.Installer.Unattended })
    $skipped = $all.Count - $todo.Count
    if (-not __BIOS__) { $todo = @($todo | Where-Object { $_.Type -ne 'BIOS' -and $_.Category -notmatch 'BIOS|UEFI' -and $_.Title -notmatch 'BIOS|UEFI' }) }
    if ($skipped -gt 0) { L ("{0} pacote(s) exigem interacao e foram ignorados." -f $skipped) }
    if ($todo.Count -eq 0) { P 100 'Nenhum driver Lenovo pendente'; Write-Output 'R|pending=0'; exit 0 }
    L ("Encontrados {0} pacote(s) Lenovo:" -f $todo.Count)
    foreach ($u in $todo) { L ('  - ' + $u.Title) }
    $i = 0; $ok = 0; $fail = 0; $reboot = $false
    foreach ($u in $todo) {
        $i++
        $base = 5 + 90 * ($i - 1) / $todo.Count
        P $base ("Baixando {0}/{1}: {2}" -f $i, $todo.Count, $u.Title)
        try { Save-LSUpdate -Package $u -Path $dl -WarningAction SilentlyContinue | Out-Null } catch { L ('Falha no download: ' + $u.Title); $fail++; continue }
        P ($base + 45 / $todo.Count) ("Instalando {0}/{1}: {2}" -f $i, $todo.Count, $u.Title)
        $wv = $null
        $out = Install-LSUpdate -Package $u -Path $dl -SaveBIOSUpdateInfoToRegistry -WarningVariable wv -WarningAction SilentlyContinue
        if ($wv -and $wv.Count -gt 0) { $fail++; L ('Falhou: ' + $u.Title + ' — ' + ($wv | Select-Object -First 1)) }
        else { $ok++; L ('OK: ' + $u.Title) }
        if ($u.RebootType -in 1,3,4 -or ($out -match 'BIOS UPDATE SUCCESS')) { $reboot = $true }
    }
    P 100 ("Lenovo: {0} instalado(s), {1} falha(s)" -f $ok, $fail)
    Write-Output ("R|pending={0}" -f $todo.Count)
    Write-Output ("R|installed={0}" -f $ok)
    Write-Output ("R|failed={0}" -f $fail)
    if ($reboot) { Write-Output 'R|reboot=1' }
} catch {
    L ('ERRO: ' + $_.Exception.Message)
    Write-Output 'R|error=1'
    exit 2
}
"""


def lenovo_update(progress: Progress, cancel: threading.Event, include_bios: bool = False) -> str:
    from .provision import _parse_wu_line
    mod = find_resource("LSUClient")
    if not mod:
        raise FileNotFoundError("Módulo LSUClient não encontrado na pasta Recursos.")
    result: dict = {}
    script = (LENOVO_PS.replace("__MOD__", ps_quote(str(mod)))
              .replace("__DL__", ps_quote(str(sub_work_dir("lenovo"))))
              .replace("__BIOS__", "$true" if include_bios else "$false"))
    stream_ps(script, lambda ln: _parse_wu_line(ln, progress, result), timeout=3 * 3600, cancel=cancel)
    if cancel.is_set():
        raise RuntimeError("Cancelado")
    if result.get("error"):
        raise RuntimeError("Falha ao consultar a Lenovo (verifique a internet). Veja o log.")
    if result.get("pending", 0) == 0:
        return "Nenhum driver Lenovo pendente."
    msg = f"{result.get('installed', 0)} instalado(s), {result.get('failed', 0)} falha(s)"
    if result.get("reboot"):
        raise NeedsReboot("Drivers Lenovo instalados; reinicialização necessária.")
    return msg


# =========================================================================== HP
HPIA_FETCH_PS = r"""
$ErrorActionPreference = 'Stop'
$dir = __DIR__
$page = (Invoke-WebRequest -UseBasicParsing 'https://ftp.ext.hp.com/pub/caps-softpaq/cmit/HPIA.html').Content
$links = [regex]::Matches($page, 'https://hpia\.hpcloud\.hp\.com/downloads/hpia/hp-hpia-([\d\.]+)\.exe') |
    Sort-Object { [version]$_.Groups[1].Value } -Descending
if (-not $links) { throw 'Link do HP Image Assistant nao encontrado no site da HP.' }
$url = $links[0].Value
$exe = Join-Path $env:TEMP ([IO.Path]::GetFileName($url))
Write-Output ('Baixando ' + $url)
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $exe
Start-Process -FilePath $exe -ArgumentList '/s','/e','/f',('"' + $dir + '"') -Wait
if (-not (Test-Path (Join-Path $dir 'HPImageAssistant.exe'))) { throw 'Falha ao extrair o HP Image Assistant.' }
Write-Output 'OK'
"""
HPIA_CODES = {0: "Drivers instalados", 256: "Nenhuma atualização recomendada", 257: "Nada selecionado",
              3010: "Instalado — reinicialização necessária", 3020: "Alguns pacotes falharam",
              4096: "Modelo não suportado pelo HPIA", 4097: "Parâmetros inválidos", 4098: "Sem internet",
              4100: "Falha ao baixar catálogo"}


def _hpia_exe(progress: Progress) -> Path:
    for name in ("HPIA/HPImageAssistant.exe", "Drivers HP/HPIA/HPImageAssistant.exe"):
        p = find_resource(name)
        if p:
            return p
    target = sub_work_dir("HPIA")
    exe = target / "HPImageAssistant.exe"
    if exe.is_file():
        return exe
    # instalador softpaq do HPIA colocado na pasta Recursos (hp-hpia-x.y.z.exe)
    for folder in ("Drivers HP", "HP", "Programas"):
        d = find_resource(folder)
        if d and d.is_dir():
            for f in d.glob("hp-hpia-*.exe"):
                log(f"Extraindo {f.name}…")
                run([str(f), "/s", "/e", "/f", str(target)], timeout=300)
                if exe.is_file():
                    return exe
    progress(-1, "Baixando HP Image Assistant do site da HP…")
    log("Baixando HP Image Assistant (ftp.ext.hp.com)…")
    r = run_ps(HPIA_FETCH_PS.replace("__DIR__", ps_quote(str(target))), timeout=900)
    for ln in r.stdout.splitlines():
        log("  " + ln)
    if not exe.is_file():
        raise RuntimeError("Não foi possível obter o HP Image Assistant: " + (r.stderr.strip()[-300:] or "sem internet?"))
    return exe


def hp_update(progress: Progress, cancel: threading.Event, include_bios: bool = False) -> str:
    exe = _hpia_exe(progress)
    rep, dl = sub_work_dir("HPIA-relatorio"), sub_work_dir("HPIA-download")
    cats = "Drivers,Software,Accessories" + (",BIOS,Firmware" if include_bios else "")
    args = [str(exe), "/Operation:Analyze", f"/Category:{cats}", "/Selection:All", "/Action:Install", "/Silent",
            "/Noninteractive", f"/ReportFolder:{rep}", f"/SoftpaqDownloadFolder:{dl}"]
    log("HP Image Assistant: analisando o modelo e instalando drivers (sem janelas)…")
    proc = subprocess.Popen(args, cwd=str(exe.parent), creationflags=CREATE_NO_WINDOW)
    rc = _wait_proc(proc, progress, "HP Image Assistant", cancel)
    msg = HPIA_CODES.get(rc, f"código {rc}")
    if rc in (0, 256, 257):
        progress(100, msg)
        log(f"HP: {msg}.", "ok")
        return msg
    if rc == 3010:
        progress(100, msg)
        raise NeedsReboot("Drivers HP instalados; reinicialização necessária.")
    if rc == 3020:
        log(f"HP: {msg}. Relatório em {rep}.", "warn")
        return msg
    raise RuntimeError(f"HP Image Assistant: {msg}.")


# =========================================================================== genérico (Positivo / outros)
def windows_update_drivers(progress: Progress, cancel: threading.Event) -> str:
    res = windows_update_round(progress, cancel, only_drivers=True)
    if res.get("pending", 0) == 0:
        return "Nenhum driver pendente no Windows Update."
    msg = f"{res.get('installed', 0)} driver(s) instalado(s), {res.get('failed', 0)} falha(s)"
    if res.get("reboot"):
        raise NeedsReboot("Drivers instalados; reinicialização necessária.")
    return msg


VENDOR_TOOL = {
    "dell": ("Dell Command | Update", dell_update),
    "lenovo": ("Catálogo Lenovo (LSUClient)", lenovo_update),
    "hp": ("HP Image Assistant", hp_update),
}


def vendor_drivers(vendor: str, progress: Progress, cancel: threading.Event, include_bios: bool = False) -> str:
    """Drivers do fabricante; para Positivo/outros usa o catálogo de drivers do Windows Update."""
    if vendor in VENDOR_TOOL:
        name, fn = VENDOR_TOOL[vendor]
        log(f"Fabricante detectado: {vendor.upper()} → {name}.")
        try:
            return fn(progress, cancel, include_bios)
        except NeedsReboot:
            raise
        except Exception as e:  # ferramenta do fabricante falhou: cai para o Windows Update
            if cancel.is_set():
                raise
            log(f"{name} falhou ({e}). Usando drivers do Windows Update como alternativa.", "warn")
            return windows_update_drivers(progress, cancel)
    label = "Positivo" if vendor == "positivo" else "fabricante não identificado"
    log(f"Fabricante: {label} → drivers pelo catálogo do Windows Update.")
    return windows_update_drivers(progress, cancel)


# =========================================================================== pacotes offline (Recursos)
OFFLINE_PACKS = [
    {"key": "dell", "vendor": "dell", "folder": "Drivers Dell", "title": "Pacote Dell (offline)",
     "desc": "Wi-Fi, Bluetooth e Intel HID — instaladores Dell embutidos.", "kind": "exe_folder"},
    {"key": "hp", "vendor": "hp", "folder": "Drivers HP", "title": "Pacote HP (offline)",
     "desc": "Wi-Fi e Bluetooth — instaladores HP embutidos.", "kind": "exe_folder"},
    {"key": "portcode", "vendor": "lenovo", "folder": "portcode Lenovo", "title": "Portcode Lenovo",
     "desc": "Aplica o mapeamento de teclado (Portcode.reg).", "kind": "reg"},
    {"key": "lenovo_su", "vendor": "lenovo", "file": "Programas/system_update_5.08.03.59.exe",
     "title": "Lenovo System Update", "desc": "Instala o utilitário oficial da Lenovo (silencioso).",
     "kind": "exe", "args": ["/VERYSILENT", "/NORESTART"]},
]

# Parâmetros silenciosos conhecidos para instaladores de driver
SILENT_ARGS = {
    "dell": ["/s"],          # Dell Update Package
    "hp": [],                # instaladores HP do pacote rodam com a janela padrão
}


def offline_available(pack: dict) -> bool:
    if pack.get("folder"):
        d = find_resource(pack["folder"])
        return bool(d and d.is_dir() and any(d.iterdir()))
    return bool(find_resource(pack.get("file", "")))


def install_offline(pack: dict, progress: Progress, cancel: threading.Event) -> str:
    kind = pack["kind"]
    if kind == "reg":
        d = find_resource(pack["folder"])
        regs = sorted(d.glob("*.reg")) if d else []
        if not regs:
            raise FileNotFoundError("Nenhum arquivo .reg no pacote.")
        for r in regs:
            progress(-1, f"Aplicando {r.name}…")
            res = run(["reg", "import", str(r)], timeout=60)
            if res.returncode != 0:
                raise RuntimeError(f"Falha ao importar {r.name}: {res.stderr.strip()}")
            log(f"{r.name} aplicado ao registro.", "ok")
        return "Registro aplicado (reinicie para valer)."
    if kind == "exe":
        f = find_resource(pack["file"])
        if not f:
            raise FileNotFoundError(f"{pack['file']} não encontrado.")
        progress(-1, f"Instalando {f.name}…")
        unblock(f)
        rc = run([str(f)] + pack.get("args", []), timeout=1800).returncode
        if rc not in (0, 3010):
            raise RuntimeError(f"{f.name} retornou código {rc}.")
        return "Instalado."
    # exe_folder: roda cada instalador em modo silencioso; .inf via pnputil
    d = find_resource(pack["folder"])
    files = sorted([f for f in d.iterdir() if f.suffix.lower() == ".exe"]) if d else []
    infs = sorted(d.rglob("*.inf")) if d else []
    if not files and not infs:
        raise FileNotFoundError("Nenhum instalador no pacote.")
    total, done, fails = len(files) + len(infs), 0, 0
    for f in files:
        if cancel.is_set():
            raise RuntimeError("Cancelado")
        progress(100 * done / total, f"Instalando {f.name}…")
        unblock(f)
        rc = run([str(f)] + SILENT_ARGS.get(pack["vendor"], ["/s"]), timeout=1200).returncode
        ok = rc in (0, 2, 3010, 1641)  # DUP: 2 = reboot necessário
        log(f"{'OK' if ok else 'Falhou'}: {f.name} (código {rc})", "ok" if ok else "warn")
        fails += 0 if ok else 1
        done += 1
    for inf in infs:
        progress(100 * done / total, f"Registrando {inf.name}…")
        rc = run(["pnputil", "/add-driver", str(inf), "/install"], timeout=600).returncode
        fails += 0 if rc in (0, 3010) else 1
        done += 1
    progress(100, "Pacote concluído")
    return "Pacote concluído" + (f" ({fails} falha(s))" if fails else "")
