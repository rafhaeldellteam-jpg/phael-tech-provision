"""Finalizar e selar: sai do modo de auditoria e entrega a máquina como 'nova de fábrica' (OOBE).

Roda o Sysprep do próprio Windows:  sysprep.exe /oobe [/generalize] /shutdown|/reboot /quiet
Antes confere o que costuma fazer o Sysprep falhar e, se falhar, mostra o motivo lido do log do Windows.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from .provision import clear_resume_on_boot, clear_state, load_state, write_report
from .util import CREATE_NO_WINDOW, IS_WIN, log, run, run_ps

SYSPREP = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "Sysprep" / "sysprep.exe"
PANTHER = SYSPREP.parent / "Panther"

CHECK_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$r = [ordered]@{}
# reinicialização pendente (o Sysprep recusa)
$pend = $false
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $pend = $true }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $pend = $true }
$pfro = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations).PendingFileRenameOperations
if ($pfro) { $pend = $true }
$r.reboot_pending = $pend
# modo de auditoria
$st = (Get-ItemProperty 'HKLM:\SYSTEM\Setup\Status\SysprepStatus').GeneralizationState
$audit = (Get-ItemProperty 'HKLM:\SYSTEM\Setup').AuditInProgress
$r.audit_mode = [bool]($audit -eq 1)
$r.generalization_state = $st
# Windows ativado?
$lic = Get-CimInstance SoftwareLicensingProduct -Filter "PartialProductKey IS NOT NULL AND ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f'" | Select-Object -First 1
$r.windows_activated = [bool]($lic -and $lic.LicenseStatus -eq 1)
# aplicativos da Store instalados só para um usuário (motivo nº 1 de falha do /generalize)
$bad = @()
try {
    $prov = @(Get-AppxProvisionedPackage -Online | ForEach-Object { $_.DisplayName })
    $bad = @(Get-AppxPackage -AllUsers | Where-Object {
        -not $_.IsFramework -and -not $_.NonRemovable -and $_.SignatureKind -ne 'System' -and
        ($prov -notcontains $_.Name) -and $_.PackageUserInformation.Count -gt 0
    } | Select-Object -ExpandProperty Name -Unique | Select-Object -First 15)
} catch {}
$r.user_appx = $bad
# BitLocker ligado/criptografando (atrapalha o /generalize)
$bl = Get-BitLockerVolume -MountPoint 'C:'
$r.bitlocker = if ($bl) { [string]$bl.VolumeStatus } else { '' }
$r.sysprep_running = [bool](Get-Process sysprep -ErrorAction SilentlyContinue)
$r | ConvertTo-Json -Compress -Depth 4
"""


def precheck() -> dict:
    """Confere o que pode impedir o Sysprep. Retorna itens com status ok/warn/bad."""
    if not IS_WIN:
        return {"items": [], "error": "Disponível apenas no Windows."}
    r = run_ps(CHECK_PS, timeout=120)
    try:
        data = json.loads(r.stdout.strip()[r.stdout.strip().find("{"):])
    except Exception:
        return {"items": [], "error": "Não foi possível verificar a máquina."}
    items = []
    items.append({"key": "audit", "label": "Modo de auditoria",
                  "status": "ok" if data.get("audit_mode") else "warn",
                  "detail": "Ativo" if data.get("audit_mode") else "A máquina não está em modo de auditoria"})
    items.append({"key": "reboot", "label": "Reinicialização pendente",
                  "status": "bad" if data.get("reboot_pending") else "ok",
                  "detail": "Reinicie antes de selar (o Sysprep recusa)" if data.get("reboot_pending") else "Nenhuma"})
    pend = load_state().get("pending")
    items.append({"key": "provision", "label": "Provisionamento",
                  "status": "warn" if pend else "ok",
                  "detail": "Ainda há etapas pendentes: " + ", ".join(pend) if pend else "Sem etapas pendentes"})
    items.append({"key": "activation", "label": "Ativação do Windows",
                  "status": "ok" if data.get("windows_activated") else "warn",
                  "detail": "Ativado" if data.get("windows_activated") else "Não ativado (ativa sozinho com internet se a licença for de fábrica)"})
    appx = data.get("user_appx") or []
    if isinstance(appx, str):
        appx = [appx]
    items.append({"key": "appx", "label": "Apps da Store por usuário",
                  "status": "warn" if appx else "ok",
                  "detail": ("Podem fazer o 'Generalizar' falhar: " + ", ".join(appx[:6])) if appx else "Nenhum conflito"})
    bl = str(data.get("bitlocker") or "")
    items.append({"key": "bitlocker", "label": "BitLocker",
                  "status": "warn" if bl and bl != "FullyDecrypted" else "ok",
                  "detail": "Desligado" if not bl or bl == "FullyDecrypted" else f"Status: {bl}"})
    return {"items": items, "can_seal": not data.get("reboot_pending"), "raw": data}


def _sysprep_errors() -> str:
    for name in ("setuperr.log", "setupact.log"):
        f = PANTHER / name
        if f.exists():
            try:
                lines = f.read_text(encoding="utf-16", errors="ignore").splitlines()
            except Exception:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            errs = [l for l in lines if "Error" in l or "Erro" in l][-6:]
            if errs:
                return "\n".join(e.strip()[-220:] for e in errs)
    return ""


def remove_user_appx(on_line: Callable[[str], None]) -> None:
    """Remove os apps da Store instalados só para um usuário (corrige a falha mais comum do Generalizar)."""
    raw = precheck().get("raw", {})
    names = raw.get("user_appx") or []
    if isinstance(names, str):
        names = [names]
    for n in names:
        on_line(f"Removendo {n}…")
        run_ps(f"Get-AppxPackage -AllUsers -Name '{n}' | Remove-AppxPackage -AllUsers -ErrorAction SilentlyContinue", timeout=180)
    on_line("Pronto.")


def seal(generalize: bool, action: str, inventory: dict | None, on_line: Callable[[str], None]) -> str:
    """action: 'shutdown' | 'reboot'. Não retorna se o Sysprep der certo (a máquina desliga)."""
    if not SYSPREP.is_file():
        raise FileNotFoundError("sysprep.exe não encontrado.")
    on_line("Salvando o relatório da máquina…")
    try:
        write_report(inventory or {})
    except Exception as e:
        on_line(f"Relatório não salvo: {e}")
    on_line("Limpando a continuação automática do programa…")
    clear_resume_on_boot()
    clear_state()
    # o Sysprep do modo de auditoria abre sozinho; com ele aberto, um segundo Sysprep falha
    if IS_WIN:
        run(["taskkill", "/IM", "sysprep.exe", "/F"], timeout=30)
        time.sleep(2)
    args = [str(SYSPREP), "/oobe", f"/{action}", "/quiet"]
    if generalize:
        args.insert(2, "/generalize")
    on_line("Executando: " + " ".join(args[1:]))
    log("Finalizar e selar: " + " ".join(args[1:]), "warn")
    started = time.time()
    proc = subprocess.Popen(args, cwd=str(SYSPREP.parent), creationflags=CREATE_NO_WINDOW)
    while proc.poll() is None:
        time.sleep(2)
        on_line(f"Sysprep trabalhando… {int(time.time() - started)} s")
    # se chegou aqui o Sysprep terminou sem desligar: algo deu errado
    errs = _sysprep_errors()
    msg = f"O Sysprep terminou sem {('desligar' if action == 'shutdown' else 'reiniciar')} (código {proc.returncode})."
    if errs:
        msg += "\n\nMotivo informado pelo Windows:\n" + errs
    raise RuntimeError(msg)


def open_gui() -> str:
    """Abre a janela normal do Sysprep (Ferramenta de Preparação do Sistema) para escolher na mão."""
    if not SYSPREP.is_file():
        raise FileNotFoundError("sysprep.exe não encontrado.")
    if IS_WIN:
        r = run(["tasklist", "/FI", "IMAGENAME eq sysprep.exe", "/NH"], timeout=20)
        if "sysprep.exe" in r.stdout.lower():
            return "O Sysprep já está aberto (veja na barra de tarefas)."
    subprocess.Popen([str(SYSPREP)], cwd=str(SYSPREP.parent))
    log("Janela do Sysprep aberta pelo programa.")
    return "Sysprep aberto."
