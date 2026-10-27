"""Atualização automática pelo GitHub Releases.

Fluxo: consulta a release mais recente -> compara a versão -> baixa o .exe para o ProgramData ->
confere tamanho e SHA-256 -> um script espera este programa fechar, troca o .exe e abre a versão nova.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .util import APP_VERSION, CREATE_NO_WINDOW, GITHUB_REPO, IS_WIN, log, ps_quote, run_ps, sub_work_dir
from .version import SIGN_THUMBPRINT

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
    newer = _ver(ver) > _ver(APP_VERSION)
    if newer and not _asset_ready(url + ".sha256"):
        # a versão acabou de ser publicada e o GitHub ainda está montando o .exe (leva ~5 min):
        # não oferece ainda — antes isso dava 'HTTP 404' e falha na atualização
        log(f"Versão {ver} publicada, mas o .exe ainda está sendo preparado no GitHub. Tento de novo depois.")
        return {"available": False, "current": APP_VERSION, "version": ver, "preparing": True,
                "error": f"A versão {ver} está sendo preparada no GitHub. Tente de novo em alguns minutos."}
    return {"available": newer, "current": APP_VERSION, "version": ver,
            "url": url, "sha_url": url + ".sha256"}


def _asset_ready(url: str) -> bool:
    """O arquivo da release já existe? (404 = ainda não foi anexado)"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with _opener().open(req, timeout=15):
            return True
    except urllib.error.HTTPError as e:
        return e.code != 404
    except Exception:
        try:
            out = _ps(f"$r = [Net.HttpWebRequest]::Create('{url}'); $r.Proxy = $proxy; $r.Method = 'HEAD'; "
                      f"$r.UserAgent = '{UA}'; try {{ $x = $r.GetResponse(); $x.Close(); 'OK' }} "
                      "catch [Net.WebException] { if ($_.Exception.Response.StatusCode.value__ -eq 404) { 'NAO' } else { 'OK' } }", 40)
            return "NAO" not in out
        except Exception:
            return True


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
    verify_signature(tmp)
    dest.unlink(missing_ok=True)
    tmp.rename(dest)
    progress(100)
    return dest


def verify_signature(path: Path) -> None:
    """Se este programa é assinado, a atualização também precisa ser: pelo MESMO certificado
    (autoassinado da Phael Tech) ou por um certificado comercial válido (caso troque no futuro)."""
    if not SIGN_THUMBPRINT or not IS_WIN:
        return
    r = run_ps(f"$s = Get-AuthenticodeSignature -LiteralPath {ps_quote(str(path))}; "
               "Write-Output ([string]$s.Status + '|' + [string]$s.SignerCertificate.Thumbprint)", timeout=60)
    status, _, thumb = r.stdout.strip().partition("|")
    if status == "Valid" or (thumb.upper() == SIGN_THUMBPRINT.upper() and status not in ("HashMismatch", "NotSigned")):
        log(f"Assinatura digital da atualização conferida ({status}).", "ok")
        return
    path.unlink(missing_ok=True)
    raise RuntimeError(f"A atualização não tem a assinatura digital da Phael Tech (status: {status or 'desconhecido'}). "
                       "Por segurança, ela não foi instalada.")


def apply(new_exe: Path) -> Path:
    """Troca o .exe NA HORA, com o programa ainda aberto.

    O Windows não deixa sobrescrever um .exe em execução, mas deixa RENOMEAR. Então:
    o atual vira "<nome>.exe.old", a versão nova é copiada no nome original e quem chama
    abre o .exe novo e fecha este. Não depende de nenhum script rodando depois que o programa fecha
    (era isso que falhava: o aviso dizia que ia reiniciar, mas a troca nunca acontecia).
    """
    if not can_self_update():
        raise RuntimeError("A atualização automática só funciona no .exe compilado.")
    target = Path(sys.executable).resolve()
    old = target.with_name(target.name + ".old")
    if old.exists():
        try:
            old.unlink()
        except OSError:
            old = target.with_name(f"{target.name}.{os.getpid()}.old")
    try:
        os.rename(target, old)
    except OSError as e:
        raise RuntimeError(f"Não foi possível renomear o programa atual ({e}). "
                           "Ele está numa pasta somente leitura?") from e
    try:
        shutil.copyfile(new_exe, target)
    except Exception as e:
        try:
            os.rename(old, target)          # desfaz: volta o programa antigo
        except OSError:
            pass
        raise RuntimeError(f"Não foi possível gravar a versão nova ({e}).") from e
    try:
        new_exe.unlink()
    except OSError:
        pass
    log(f"Atualização aplicada em {target}. Reabrindo na versão nova…", "ok")
    return target


def launch(target: Path) -> None:
    """Abre o .exe novo como um processo independente, avisando quais processos ele deve esperar
    fechar antes de criar a janela (este e o lançador do PyInstaller, que é o processo pai)."""
    DETACHED = 0x00000008
    NEW_GROUP = 0x00000200
    arg = f"--apos-atualizar={os.getpid()},{os.getppid()}"
    subprocess.Popen([str(target), arg], cwd=str(target.parent), close_fds=True,
                     creationflags=DETACHED | NEW_GROUP)


def wait_previous_instance(argv: list[str], webview_dir: Path, limit: float = 25.0) -> None:
    """Chamado pela versão NOVA ao abrir depois de uma atualização: espera a versão antiga e o
    navegador interno dela (msedgewebview2) saírem de vez. Se a janela nova abrisse antes, as duas
    disputavam a mesma pasta do WebView2 e a interface travava (era o 'bugou' depois de atualizar)."""
    import time
    pids = []
    for a in argv:
        if a.startswith("--apos-atualizar="):
            pids = [int(x) for x in a.split("=", 1)[1].split(",") if x.strip().isdigit()]
    if not pids:
        return
    try:
        import psutil
    except ImportError:
        time.sleep(8)
        return
    start = time.time()
    while time.time() - start < limit:
        alive = [p for p in pids if psutil.pid_exists(p)]
        marker = str(webview_dir).lower()
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                if (proc.info["name"] or "").lower() == "msedgewebview2.exe" and \
                        marker in " ".join(proc.info["cmdline"] or []).lower():
                    alive.append(proc.pid)
            except (psutil.Error, OSError):
                continue
        if not alive:
            break
        time.sleep(0.5)
    time.sleep(1.0)


def cleanup_old_copies() -> None:
    """Apaga os '.exe.old' deixados por atualizações anteriores (tenta por até 2 minutos)."""
    if not can_self_update():
        return
    import time
    me = Path(sys.executable).resolve()
    for _ in range(24):
        left = 0
        for f in me.parent.glob(me.name + "*.old"):
            try:
                f.unlink()
            except OSError:
                left += 1
        if not left:
            return
        time.sleep(5)


def kill_own_children() -> None:
    """Fecha na hora os processos filhos deste programa (navegador interno WebView2, PowerShell dos
    sensores), para a versão nova não precisar esperar por eles."""
    try:
        import psutil
        me = psutil.Process()
        kids = me.children(recursive=True)
        for k in kids:
            try:
                k.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(kids, timeout=5)
    except Exception:
        pass
