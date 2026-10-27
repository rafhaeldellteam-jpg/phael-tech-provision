"""Etapas do provisionamento: energia, Office, Windows Update (com drivers) e finalização."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .util import (IS_WIN, find_resource, log, run, run_ps, stream_ps, sub_work_dir, task_names, unblock,
                   work_dir)

Progress = Callable[[float, str], None]   # (0-100 ou -1 = indeterminado, texto)

RUNONCE_KEY = r"HKLM\Software\Microsoft\Windows\CurrentVersion\RunOnce"
RUNONCE_NAME = "PhaelTechProvision"
STATE_FILE = "estado.json"


class NeedsReboot(Exception):
    """A etapa terminou parcialmente e precisa reiniciar para continuar."""


# =========================================================================== estado / continuidade
def load_state() -> dict:
    p = work_dir() / STATE_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    (work_dir() / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_state() -> None:
    try:
        (work_dir() / STATE_FILE).unlink()
    except OSError:
        pass


def _self_command(arg: str) -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" {arg}'
    return f'"{sys.executable}" "{Path(sys.argv[0]).resolve()}" {arg}'


def set_resume_on_boot() -> bool:
    if not IS_WIN:
        return False
    r = run(["reg", "add", RUNONCE_KEY, "/v", RUNONCE_NAME, "/t", "REG_SZ", "/d", _self_command("--continuar"), "/f"])
    return r.returncode == 0


def clear_resume_on_boot() -> None:
    if IS_WIN:
        run(["reg", "delete", RUNONCE_KEY, "/v", RUNONCE_NAME, "/f"])


def reboot(seconds: int = 10, msg: str = "Reiniciando para continuar o provisionamento") -> None:
    if IS_WIN:
        run(["shutdown", "/r", "/t", str(seconds), "/c", msg])


# =========================================================================== 1. energia
POWER_PS = r"""
$ErrorActionPreference = 'Stop'
$path = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power'
if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }
New-ItemProperty -Path $path -Name HiberbootEnabled -Value 0 -PropertyType DWord -Force | Out-Null
# Politica: garante que o Painel de Controle nao reative a inicializacao rapida
$pol = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System'
if (-not (Test-Path $pol)) { New-Item -Path $pol -Force | Out-Null }
New-ItemProperty -Path $pol -Name HiberbootEnabled -Value 0 -PropertyType DWord -Force | Out-Null
__EXTRA__
$check = (Get-ItemProperty -Path $path -Name HiberbootEnabled).HiberbootEnabled
if ($check -ne 0) { throw 'O Windows nao aceitou a alteracao (HiberbootEnabled continua ativo).' }
Write-Output 'OK'
"""

POWER_EXTRA_BUTTON = r"""
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3 | Out-Null
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3 | Out-Null
"""
POWER_EXTRA_NOSLEEP = r"""
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null
powercfg /change monitor-timeout-ac 30 | Out-Null
"""


def trust_certificate() -> str:
    """Instala o certificado PÚBLICO da Phael Tech como confiável nesta máquina, para o Windows
    mostrar 'Editor: Phael Tech' em vez de 'Editor desconhecido' ao abrir o programa."""
    cer = find_resource("phaeltech.cer")
    if not cer:
        return "Este .exe não tem certificado embutido (versão sem assinatura)."
    r = run_ps(f"""
$ErrorActionPreference = 'Stop'
Import-Certificate -FilePath '{cer}' -CertStoreLocation Cert:\\LocalMachine\\Root | Out-Null
Import-Certificate -FilePath '{cer}' -CertStoreLocation Cert:\\LocalMachine\\TrustedPublisher | Out-Null
Write-Output 'OK'
""", timeout=60)
    if "OK" not in r.stdout:
        raise RuntimeError("Não foi possível instalar o certificado: " + (r.stderr.strip()[-200:] or "erro"))
    log("Certificado digital da Phael Tech marcado como confiável nesta máquina.", "ok")
    return "Certificado instalado."


def disable_fast_startup(progress: Progress, power_button_shutdown: bool = True, no_sleep_ac: bool = True) -> str:
    progress(-1, "Desativando inicialização rápida…")
    extra = ""
    if power_button_shutdown:
        extra += POWER_EXTRA_BUTTON
    if no_sleep_ac:
        extra += POWER_EXTRA_NOSLEEP
    extra += "powercfg /setactive SCHEME_CURRENT | Out-Null\n"
    r = run_ps(POWER_PS.replace("__EXTRA__", extra), timeout=60)
    if r.returncode != 0 or "OK" not in r.stdout:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                           else "Falha ao aplicar as configurações de energia.")
    log("Inicialização rápida desativada (HiberbootEnabled=0, conferido no registro).", "ok")
    if power_button_shutdown:
        log("Botão liga/desliga configurado para Desligar.")
    if no_sleep_ac:
        log("Suspensão na tomada desativada (evita parar as atualizações).")
    progress(100, "Recurso desativado")
    return "Recurso desativado."


# =========================================================================== 2. Office
OFFICE_NAMES = ("OfficeSetup.exe", "Office/OfficeSetup.exe", "Office/setup.exe")
OFFICE_PROCS = {"officesetup.exe", "setup.exe", "officec2rclient.exe", "integratedoffice.exe"}


def office_installed() -> str:
    """Retorna a versão/produtos do Office Click-to-Run instalados ('' se não houver)."""
    if not IS_WIN:
        return ""
    r = run_ps(r"""
$c = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Office\ClickToRun\Configuration' -ErrorAction SilentlyContinue
if ($c -and $c.ProductReleaseIds) { Write-Output ($c.ProductReleaseIds + '|' + $c.VersionToReport) }
""", timeout=30)
    return r.stdout.strip()


def _c2r_busy() -> bool:
    """Instalação C2R em andamento? (processos do instalador ativos)"""
    names = task_names()
    return bool(names & OFFICE_PROCS)


def install_office(progress: Progress, cancel: threading.Event, force: bool = False) -> str:
    already = office_installed()
    if already and not force:
        prods = already.split("|")[0]
        log(f"Office já instalado ({prods}). Etapa ignorada.", "ok")
        progress(100, "Já instalado")
        return f"Já instalado: {prods}"
    src = find_resource(*OFFICE_NAMES)
    if not src:
        raise FileNotFoundError("Instalador do Office (OfficeSetup.exe) não encontrado na pasta Recursos.")
    dst = sub_work_dir("instaladores") / src.name
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        shutil.copyfile(src, dst)
    unblock(dst)
    progress(-1, "Abrindo instalador do Microsoft 365…")
    log(f"Iniciando {src.name} (requer internet — o instalador baixa o Office da Microsoft).")
    subprocess.Popen([str(dst)], cwd=str(dst.parent))
    # Aguarda o instalador aparecer
    t0 = time.time()
    while time.time() - t0 < 60 and not _c2r_busy():
        if cancel.wait(1):
            raise RuntimeError("Cancelado")
    # Aguarda terminar: processos do instalador encerrados + Office registrado
    start = time.time()
    last_note = 0.0
    while True:
        if cancel.wait(5):
            raise RuntimeError("Cancelado")
        elapsed = time.time() - start
        busy = _c2r_busy()
        inst = office_installed()
        if inst and not busy:
            prods = inst.split("|")[0]
            log(f"Office instalado com sucesso ({prods}).", "ok")
            progress(100, "Office instalado")
            return "Office instalado com sucesso."
        if not busy and elapsed > 90 and not inst:
            raise RuntimeError("O instalador do Office fechou sem concluir. Verifique a internet e tente novamente.")
        if elapsed > 3600:
            raise RuntimeError("Instalação do Office passou de 60 minutos. Verifique a janela do instalador.")
        # progresso estimado (o C2R não informa %): curva assintótica até 95%
        pct = min(95.0, 95 * (1 - pow(2.71828, -elapsed / 420)))
        progress(pct, f"Instalando Office… {int(elapsed // 60)} min")
        if elapsed - last_note > 300:
            last_note = elapsed
            log("Office ainda instalando (a duração depende da internet)…")


# =========================================================================== 3. Windows Update
# Saída padronizada lida pelo Python:
#   P|<pct>|<texto>  -> progresso      L|<texto> -> log      R|<chave>=<valor> -> resultado
WU_PS = r"""
$ErrorActionPreference = 'Stop'
function P($pct, $t) { Write-Output ("P|{0}|{1}" -f [int]$pct, $t) }
function L($t) { Write-Output ("L|" + $t) }
$onlyDrivers = __ONLY_DRIVERS__
try {
    # Garante os servicos necessarios
    foreach ($s in 'wuauserv','bits','cryptsvc') { try { Set-Service $s -StartupType Manual -ErrorAction SilentlyContinue; Start-Service $s -ErrorAction SilentlyContinue } catch {} }
    $session = New-Object -ComObject Microsoft.Update.Session
    $session.ClientApplicationID = 'Phael Tech Provision'
    $searcher = $session.CreateUpdateSearcher()
    $muId = '7971f918-a847-4430-9279-4a52d1efe18d'
    $useMU = $false
    if (-not $onlyDrivers) {
        try {
            $sm = New-Object -ComObject Microsoft.Update.ServiceManager
            $sm.ClientApplicationID = 'Phael Tech Provision'
            $null = $sm.AddService2($muId, 7, '')
            $useMU = $true
        } catch { L 'Microsoft Update (Office) indisponivel; usando apenas Windows Update.' }
    }
    if ($useMU) { $searcher.ServerSelection = 3; $searcher.ServiceID = $muId } else { $searcher.ServerSelection = 2 }
    $criteria = if ($onlyDrivers) { "IsInstalled=0 and IsHidden=0 and Type='Driver'" } else { "IsInstalled=0 and IsHidden=0" }
    P 2 'Procurando atualizacoes (Windows + drivers)...'
    $result = $searcher.Search($criteria)
    $skip = @(__SKIP__)
    $list = @()
    foreach ($u in $result.Updates) {
        $id = [string]$u.Identity.UpdateID
        # Já tentada 2 vezes neste provisionamento e voltou de novo: esconde para não entrar em loop
        if ($skip -contains $id) {
            L ('Ignorada (ja instalada antes e oferecida de novo): ' + $u.Title)
            try { $u.IsHidden = $true } catch {}
            continue
        }
        # Drivers costumam marcar CanRequestUserInput mesmo instalando em silencio (o próprio Windows Update instala),
        # então NÃO são mais ignorados; só pacotes de software que pedem interação ficam de fora.
        if ($u.Type -ne 2 -and $u.InstallationBehavior.CanRequestUserInput) { L ('Ignorada (pede interacao): ' + $u.Title); continue }
        if (-not $u.EulaAccepted) { try { $u.AcceptEula() } catch {} }
        $list += $u
    }
    $total = $list.Count
    if ($total -eq 0) { P 100 'Nenhuma atualizacao pendente'; Write-Output 'R|pending=0'; exit 0 }
    $drv = @($list | Where-Object { $_.Type -eq 2 }).Count
    L ("Encontradas {0} atualizacao(oes) ({1} driver(s))." -f $total, $drv)
    foreach ($u in $list) { L ('  - ' + $u.Title) }
    $ok = 0; $fail = 0; $reboot = $false; $i = 0
    foreach ($u in $list) {
        $i++
        $base = 5 + (90 * ($i - 1) / $total)
        $step = 90 / $total
        $title = $u.Title
        if ($title.Length -gt 90) { $title = $title.Substring(0, 87) + '...' }
        $coll = New-Object -ComObject Microsoft.Update.UpdateColl
        $null = $coll.Add($u)
        if (-not $u.IsDownloaded) {
            P ($base) ("Baixando {0}/{1}: {2}" -f $i, $total, $title)
            $dl = $session.CreateUpdateDownloader(); $dl.Updates = $coll
            try { $null = $dl.Download() } catch { L ('Falha no download: ' + $u.Title + ' (' + $_.Exception.Message + ')'); $fail++; continue }
        }
        Write-Output ('R|done=' + [string]$u.Identity.UpdateID)
        P ($base + $step / 2) ("Instalando {0}/{1}: {2}" -f $i, $total, $title)
        $ins = $session.CreateUpdateInstaller(); $ins.Updates = $coll
        try { $ins.ForceQuiet = $true } catch {}
        try {
            $res = $ins.Install()
            $code = $res.GetUpdateResult(0).ResultCode
            if ($code -eq 2 -or $code -eq 3) { $ok++; L ('OK: ' + $u.Title) } else { $fail++; L ('Falhou (codigo ' + $code + '): ' + $u.Title) }
            if ($res.RebootRequired) { $reboot = $true }
        } catch { $fail++; L ('Erro ao instalar: ' + $u.Title + ' (' + $_.Exception.Message + ')') }
    }
    try { if ((New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired) { $reboot = $true } } catch {}
    P 100 ("Concluido: {0} instalada(s), {1} falha(s)" -f $ok, $fail)
    Write-Output ("R|pending={0}" -f $total)
    Write-Output ("R|installed={0}" -f $ok)
    Write-Output ("R|failed={0}" -f $fail)
    if ($reboot) { Write-Output 'R|reboot=1' }
} catch {
    Write-Output ('L|ERRO: ' + $_.Exception.Message)
    Write-Output 'R|error=1'
    exit 2
}
"""


def _parse_wu_line(line: str, progress: Progress, result: dict) -> None:
    if line.startswith("P|"):
        _, pct, text = (line.split("|", 2) + ["", ""])[:3]
        try:
            progress(float(pct), text)
        except ValueError:
            progress(-1, text)
    elif line.startswith("L|"):
        msg = line[2:]
        log(msg, "error" if msg.startswith("ERRO") or msg.startswith("Falhou") else "info")
    elif line.startswith("R|") and "=" in line:
        k, v = line[2:].split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "done":                      # lista de itens tentados (para não repetir em loop)
            result.setdefault("done", []).append(v)
        else:
            result[k] = int(v) if v.lstrip("-").isdigit() else v
    else:
        log(line)


def re_guid(v: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", v or ""))


def _bump_attempts(key: str, ids: list[str]) -> None:
    """Conta quantas vezes cada item foi instalado neste provisionamento (fica salvo entre reinícios)."""
    if not ids:
        return
    state = load_state()
    seen = state.setdefault(key, {})
    for i in ids:
        seen[i] = int(seen.get(i, 0)) + 1
    save_state(state)


def _repeated(key: str, limit: int = 2) -> list[str]:
    return [i for i, n in load_state().get(key, {}).items() if int(n) >= limit]


def windows_update_round(progress: Progress, cancel: threading.Event, only_drivers: bool = False) -> dict:
    result: dict = {}
    skip = ",".join(f"'{i}'" for i in _repeated("wu_attempts") if re_guid(i))
    script = (WU_PS.replace("__ONLY_DRIVERS__", "$true" if only_drivers else "$false")
              .replace("__SKIP__", skip))
    rc = stream_ps(script, lambda ln: _parse_wu_line(ln, progress, result), timeout=4 * 3600, cancel=cancel)
    _bump_attempts("wu_attempts", result.get("done", []))
    if cancel.is_set():
        raise RuntimeError("Cancelado")
    if rc != 0 and result.get("error"):
        raise RuntimeError("O Windows Update retornou erro. Confira o log.")
    return result


def show_windows_update(scan: bool = True) -> None:
    """Abre Configurações > Windows Update e clica em 'Verificar atualizações' (o usuário vê tudo)."""
    if not IS_WIN:
        return
    try:
        os.startfile("ms-settings:windowsupdate")
    except OSError:
        pass
    if scan:
        uso = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "UsoClient.exe"
        if uso.is_file():
            run([str(uso), "StartInteractiveScan"], timeout=60)


def windows_update(progress: Progress, cancel: threading.Event, max_rounds: int = 8) -> str:
    """Abre o Windows Update nas Configurações, procura e instala TUDO (inclusive drivers).
    Repete a busca até não sobrar nada; o que falhar é tentado de novo. Se precisar reiniciar,
    levanta NeedsReboot e o programa continua sozinho depois."""
    state = load_state()
    rounds_done = int(state.get("wu_rounds", 0))
    if rounds_done >= 15:
        log("Limite de 15 ciclos do Windows Update atingido — seguindo para a próxima etapa.", "warn")
        return "Limite de ciclos atingido; confira o Windows Update manualmente."
    progress(-1, "Abrindo o Windows Update…")
    # A tela fica aberta para acompanhar, mas quem instala é o programa: se a tela também
    # instalasse ao mesmo tempo, as duas disputavam a mesma atualização e ambas falhavam
    # (era o que acontecia com a atualização cumulativa — código 4).
    log("Abrindo Configurações > Windows Update (a instalação é feita pelo programa).")
    show_windows_update(scan=False)
    retries = 0
    for rnd in range(1, max_rounds + 1):
        log(f"Windows Update — verificação {rnd} (ciclo total {rounds_done + rnd}).")
        res = windows_update_round(progress, cancel)
        if res.get("pending", 0) == 0:
            progress(100, "Sistema totalmente atualizado")
            log("Nenhuma atualização pendente. Windows totalmente atualizado.", "ok")
            show_windows_update(scan=True)   # a tela também mostra 'Você está atualizado'
            return "Sistema totalmente atualizado."
        if res.get("reboot"):
            state = load_state()
            state["wu_rounds"] = rounds_done + rnd
            save_state(state)
            raise NeedsReboot("Atualizações instaladas; é preciso reiniciar para continuar.")
        if res.get("installed", 0) == 0:
            # nada entrou nesta rodada (ex.: a tela do Windows Update estava instalando ao mesmo tempo):
            # espera e repete até 3 vezes antes de desistir
            retries += 1
            if retries > 3:
                return f"{res.get('failed', 0)} atualização(ões) não puderam ser instaladas após 3 tentativas. Veja o log."
            log(f"{res.get('failed', 0)} atualização(ões) falharam; tentando de novo em 60 s ({retries}/3).", "warn")
            progress(-1, f"Repetindo as que falharam ({retries}/3)…")
            if cancel.wait(60):
                raise RuntimeError("Cancelado")
        else:
            retries = 0
    return "Atualizações aplicadas (limite de verificações atingido)."


# =========================================================================== 4. finalização
def finalize(progress: Progress, inventory: dict | None) -> str:
    progress(20, "Limpando arquivos temporários…")
    freed = 0
    for d in [Path(os.environ.get("TEMP", "")), Path(r"C:\Windows\Temp"), sub_work_dir("instaladores"),
              sub_work_dir("scripts")]:
        freed += _clean_dir(d)
    progress(60, "Gerando relatório da máquina…")
    report = write_report(inventory or {})
    clear_resume_on_boot()
    progress(100, "Máquina pronta")
    log(f"Temporários limpos ({freed / 1024 ** 2:.0f} MB). Relatório salvo em {report}.", "ok")
    return "Relatório salvo na Área de Trabalho."


def _protected(p: Path) -> bool:
    """Nunca apaga a pasta do próprio programa (_MEI do PyInstaller fica dentro do TEMP)."""
    from .util import app_dir, bundle_dir
    try:
        rp = p.resolve()
        return any(rp == b.resolve() or b.resolve() in rp.parents for b in (bundle_dir(), app_dir())) \
            or p.name.lower().startswith("_mei")
    except OSError:
        return True


def _clean_dir(path: Path) -> int:
    freed = 0
    if not path or not path.is_dir() or _protected(path):
        return 0
    for item in list(path.iterdir()):
        if _protected(item):
            continue
        try:
            if item.is_dir() and not item.is_symlink():
                freed += _clean_dir(item)
                item.rmdir()
            else:
                size = item.stat().st_size
                item.unlink()
                freed += size
        except OSError:
            continue
    return freed


def write_report(inv: dict) -> str:
    from datetime import datetime
    lines = [f"PHAEL TECH PROVISION — Relatório de provisionamento ({datetime.now():%d/%m/%Y %H:%M})", "=" * 72]
    if inv:
        ram = inv.get("ram", {})
        lines += [
            f"Computador ........ {inv.get('hostname', '')}",
            f"Fabricante/Modelo . {inv.get('manufacturer', '')} {inv.get('model', '')}",
            f"{inv.get('serial_label', 'Serial'):<18}: {inv.get('serial', '')}",
            f"Patrimônio ........ {inv.get('asset_tag', '') or '—'}",
            f"Sistema ........... {inv.get('os', {}).get('caption', '')} {inv.get('os', {}).get('display', '')} "
            f"(build {inv.get('os', {}).get('build', '')})",
            f"Processador ....... {inv.get('cpu', {}).get('name', '')}",
            f"Placa-mãe ......... {inv.get('board', {}).get('maker', '')} {inv.get('board', {}).get('product', '')}",
            f"Memória ........... {ram.get('installed', 0) / 1024 ** 3:.0f} GB em {len(ram.get('sticks', []))} pente(s)"
            f" de {ram.get('slots', 0)} slot(s)",
            f"Áudio ............. {', '.join(a.get('name', '') for a in inv.get('audio', []))}",
            f"Vídeo ............. {', '.join(g.get('name', '') for g in inv.get('gpus', []))}",
        ]
        for d in inv.get("disks", []):
            lines.append(f"Disco ............. {d.get('model')} — {d.get('size', 0) / 1e9:.0f} GB — {d.get('health')}")
    lines += ["", "LOG DA SESSÃO", "-" * 72]
    try:
        lines += log.path.read_text(encoding="utf-8").splitlines()[-400:]
    except OSError:
        pass
    text = "\n".join(lines)
    out_dir = Path(os.environ.get("PUBLIC", "")) / "Desktop" if IS_WIN else work_dir()
    if not out_dir.is_dir():
        out_dir = work_dir()
    name = f"Provisionamento-{inv.get('hostname', 'PC')}-{datetime.now():%Y%m%d-%H%M}.txt"
    target = out_dir / name
    target.write_text(text, encoding="utf-8-sig")
    (work_dir() / name).write_text(text, encoding="utf-8-sig")
    return str(target)
