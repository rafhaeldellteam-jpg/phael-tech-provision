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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .util import APP_VERSION, CREATE_NO_WINDOW, GITHUB_REPO, IS_WIN, log, ps_quote, run_ps, sub_work_dir

API = "https://api.github.com/repos/{repo}/releases/latest"
LATEST = "https://github.com/{repo}/releases/latest"
ASSET = "https://github.com/{repo}/releases/download/v{ver}/PhaelTechProvision.exe"
UA = "PhaelTechProvision-Updater"


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


# --------------------------------------------------------------------------- rede
# Só precisa de internet: usa as páginas públicas do GitHub (sem login e sem a API, que tem limite
# de 60 consultas/hora por IP — numa empresa todas as máquinas saem pelo mesmo IP).
# Primeiro tenta o Python; se falhar (proxy da empresa, certificado), usa o .NET do Windows,
# que respeita o proxy/PAC configurado no sistema.

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _opener(follow: bool = True):
    handlers = [urllib.request.ProxyHandler(urllib.request.getproxies())]
    if not follow:
        handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers)


PS_HEAD = r"""
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
$proxy = [Net.WebRequest]::GetSystemWebProxy(); $proxy.Credentials = [Net.CredentialCache]::DefaultCredentials
"""


def _ps(script: str, timeout: int) -> str:
    if not IS_WIN:
        raise RuntimeError("sem PowerShell")
    r = run_ps(PS_HEAD + script, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "falha")
    return r.stdout.strip()


def _latest_tag() -> str:
    url = LATEST.format(repo=GITHUB_REPO)
    errors = []
    # 1) Python: lê o redirecionamento /releases/latest -> /releases/tag/vX.Y.Z
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        try:
            r = _opener(follow=False).open(req, timeout=15)
            loc = r.headers.get("Location") or r.geturl()
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            loc = e.headers.get("Location", "")
        if "/tag/" in loc:
            return loc.rsplit("/tag/", 1)[1].strip("/")
        errors.append(f"resposta inesperada ({loc or 'sem destino'})")
    except Exception as e:
        errors.append(f"Python: {e}")
    # 2) .NET do Windows (proxy do sistema)
    try:
        out = _ps(f"""
$req = [Net.HttpWebRequest]::Create('{url}'); $req.Proxy = $proxy; $req.UserAgent = '{UA}'
$req.AllowAutoRedirect = $false; $req.Method = 'HEAD'; $req.Timeout = 15000
$resp = $req.GetResponse(); Write-Output $resp.Headers['Location']; $resp.Close()
""", 40)
        if "/tag/" in out:
            return out.rsplit("/tag/", 1)[1].strip().strip("/")
        errors.append(f"Windows: resposta inesperada ({out})")
    except Exception as e:
        errors.append(f"Windows: {e}")
    # 3) API (último recurso)
    try:
        req = urllib.request.Request(API.format(repo=GITHUB_REPO), headers={"User-Agent": UA})
        with _opener().open(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))["tag_name"]
    except Exception as e:
        errors.append(f"API: {e}")
    raise RuntimeError(" | ".join(errors))


def _get_text(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _opener().open(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return _ps(f"$wc = New-Object Net.WebClient; $wc.Proxy = $proxy; $wc.Headers['User-Agent'] = '{UA}'; "
                   f"$wc.DownloadString('{url}')", 60)


def check() -> dict:
    """{'available': bool, 'version', 'url', 'sha256'} ou {'available': False, 'error'}."""
    if not configured():
        return {"available": False, "error": "Esta cópia do programa não tem o endereço de atualização. "
                                             "Baixe o .exe pela página de releases do GitHub uma vez."}
    try:
        tag = _latest_tag()
    except Exception as e:
        log(f"Não foi possível consultar a versão mais recente: {e}", "warn")
        return {"available": False, "error": "Sem conexão com a internet (ou o acesso ao GitHub está bloqueado na rede)."}
    ver = tag.lstrip("vV")
    url = ASSET.format(repo=GITHUB_REPO, ver=ver)
    return {"available": _ver(ver) > _ver(APP_VERSION), "current": APP_VERSION, "version": ver,
            "url": url, "sha_url": url + ".sha256"}


def download(info: dict, progress: Callable[[float], None]) -> Path:
    dest = sub_work_dir("atualizacao") / f"PhaelTechProvision-{info['version']}.exe"
    tmp = dest.with_suffix(".part")
    tmp.unlink(missing_ok=True)
    try:
        req = urllib.request.Request(info["url"], headers={"User-Agent": UA})
        with _opener().open(req, timeout=30) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    progress(100 * done / total)
            if total and done != total:
                raise RuntimeError("download incompleto")
    except Exception as e:
        log(f"Download pelo Python falhou ({e}); tentando pelo Windows…", "warn")
        tmp.unlink(missing_ok=True)
        progress(-1)
        _ps(f"$wc = New-Object Net.WebClient; $wc.Proxy = $proxy; $wc.Headers['User-Agent'] = '{UA}'; "
            f"$wc.DownloadFile('{info['url']}', {ps_quote(str(tmp))})", 1800)
    if not tmp.exists() or tmp.stat().st_size < 1_000_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("O arquivo baixado está incompleto.")
    # confere o SHA-256 publicado junto com a release
    try:
        expected = _get_text(info["sha_url"]).strip().split()[0].lower()
    except Exception:
        expected = ""
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    if expected and len(expected) == 64 and h.hexdigest() != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("O arquivo baixado não confere com o publicado no GitHub (SHA-256).")
    if not expected:
        log("Não consegui ler o SHA-256 da release; seguindo só com a conferência de tamanho.", "warn")
    dest.unlink(missing_ok=True)
    tmp.rename(dest)
    progress(100)
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
