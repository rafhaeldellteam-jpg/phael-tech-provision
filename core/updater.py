"""Atualização automática pelo GitHub Releases.

Fluxo: consulta a release mais recente -> compara a versão -> baixa o .exe para o ProgramData ->
confere tamanho e SHA-256 -> um script espera este programa fechar, troca o .exe e abre a versão nova.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable

from .util import APP_VERSION, CREATE_NO_WINDOW, GITHUB_REPO, IS_WIN, log, sub_work_dir

API = "https://api.github.com/repos/{repo}/releases/latest"
UA = {"User-Agent": "PhaelTechProvision-Updater", "Accept": "application/vnd.github+json"}


def _ver(s: str) -> tuple:
    parts = []
    for p in str(s).strip().lstrip("vV").split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple((parts + [0, 0, 0])[:3])


def configured() -> bool:
    return "/" in GITHUB_REPO and not GITHUB_REPO.startswith("OWNER/")


def can_self_update() -> bool:
    return IS_WIN and getattr(sys, "frozen", False)


def check() -> dict:
    """{'available': bool, 'version', 'notes', 'url', 'size', 'sha256'} ou {'error'}."""
    if not configured():
        return {"available": False, "error": "Repositório de atualização não configurado."}
    try:
        req = urllib.request.Request(API.format(repo=GITHUB_REPO), headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"available": False, "error": f"Sem acesso ao GitHub ({e.__class__.__name__})."}
    tag = rel.get("tag_name", "")
    asset = next((a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".exe")), None)
    if not asset:
        return {"available": False, "error": "A release mais recente não tem .exe."}
    digest = str(asset.get("digest") or "")
    return {
        "available": _ver(tag) > _ver(APP_VERSION),
        "current": APP_VERSION,
        "version": tag.lstrip("vV"),
        "notes": (rel.get("body") or "")[:1500],
        "url": asset["browser_download_url"],
        "size": int(asset.get("size") or 0),
        "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "",
    }


def download(info: dict, progress: Callable[[float], None]) -> Path:
    dest = sub_work_dir("atualizacao") / f"PhaelTechProvision-{info['version']}.exe"
    tmp = dest.with_suffix(".part")
    h = hashlib.sha256()
    done = 0
    req = urllib.request.Request(info["url"], headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
        total = info.get("size") or int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total:
                progress(100 * done / total)
    if info.get("size") and done != info["size"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Download incompleto.")
    if info.get("sha256") and h.hexdigest() != info["sha256"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("O arquivo baixado não confere com o SHA-256 da release.")
    if dest.exists():
        dest.unlink()
    tmp.rename(dest)
    return dest


SWAP_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$pidOld = __PID__
$target = __TARGET__
$new = __NEW__
for ($i = 0; $i -lt 120; $i++) { if (-not (Get-Process -Id $pidOld -ErrorAction SilentlyContinue)) { break }; Start-Sleep -Milliseconds 500 }
Start-Sleep -Seconds 1
$backup = $target + '.old'
$ok = $false
for ($i = 0; $i -lt 20 -and -not $ok; $i++) {
    try {
        if (Test-Path $backup) { Remove-Item $backup -Force }
        Move-Item -LiteralPath $target -Destination $backup -Force -ErrorAction Stop
        Copy-Item -LiteralPath $new -Destination $target -Force -ErrorAction Stop
        $ok = $true
    } catch {
        if ((-not (Test-Path $target)) -and (Test-Path $backup)) { Move-Item -LiteralPath $backup -Destination $target -Force }
        Start-Sleep -Seconds 1
    }
}
Start-Process -FilePath $target
if ($ok) { Start-Sleep -Seconds 5; Remove-Item $backup -Force; Remove-Item $new -Force }
"""


def apply(new_exe: Path) -> None:
    """Agenda a troca do .exe e retorna; quem chama deve fechar o programa em seguida."""
    if not can_self_update():
        raise RuntimeError("A atualização automática só funciona no .exe compilado.")
    from .util import ps_quote
    target = Path(sys.executable).resolve()
    if not os.access(target.parent, os.W_OK):
        raise RuntimeError(f"Sem permissão para gravar em {target.parent}.")
    script = (SWAP_PS.replace("__PID__", str(os.getpid())).replace("__TARGET__", ps_quote(str(target)))
              .replace("__NEW__", ps_quote(str(new_exe))))
    ps1 = sub_work_dir("atualizacao") / "aplicar.ps1"
    ps1.write_text(script, encoding="utf-8-sig")
    DETACHED = 0x00000008
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(ps1)], creationflags=CREATE_NO_WINDOW | DETACHED, close_fds=True)
    log(f"Atualização agendada: {target.name} será substituído e reaberto.", "ok")
