"""Pacotes de drivers OFFLINE por modelo (Dell, Lenovo, HP; Positivo/outros manual).

Como funciona:
  1. Numa máquina COM internet de cada modelo, clique em "Baixar pacote deste modelo".
     O programa consulta o catálogo oficial do fabricante, baixa o pacote completo de drivers
     (o mesmo usado pelas equipes de TI para imagens) e extrai na pasta
        <pasta do programa>\\Drivers por modelo\\<Fabricante>\\<ID do modelo>\\
     — normalmente o pendrive onde está o PhaelTechProvision.exe.
  2. Em qualquer máquina do mesmo modelo, mesmo SEM internet, o programa encontra essa pasta
     e instala todos os drivers com o pnputil (o instalador de drivers do próprio Windows).

ID do modelo (é o que os catálogos usam): Dell = SystemSKU (ex. 0C0B), Lenovo = 4 primeiros
caracteres do tipo da máquina (ex. 21CB), HP = Product da placa-mãe (ex. 8A78).
Os pacotes têm de 1 a 3 GB, por isso ficam numa pasta ao lado do .exe e não dentro dele.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .provision import Progress
from .util import CREATE_NO_WINDOW, IS_WIN, app_dir, log, run, stream_cmd, sub_work_dir

PACKS_DIR_NAME = "Drivers por modelo"
VENDOR_DIR = {"dell": "Dell", "lenovo": "Lenovo", "hp": "HP", "positivo": "Positivo", "other": "Outros"}
CATALOGS = {
    "dell": "https://downloads.dell.com/catalog/DriverPackCatalog.cab",
    "lenovo": "https://download.lenovo.com/cdrt/td/catalogv2.xml",
    "hp": "https://hpia.hpcloud.hp.com/downloads/driverpackcatalog/HPClientDriverPackCatalog.cab",
}
UA = "PhaelTechProvision-DriverPacks"


# --------------------------------------------------------------------------- identificação
def model_id(inv: dict) -> str:
    v = inv.get("vendor", "other")
    if v == "dell":
        return (inv.get("sku") or "").strip().upper()
    if v == "lenovo":
        raw = inv.get("model_raw") or inv.get("model", "")
        m = re.search(r"\(([0-9A-Z]{4})", raw) or re.match(r"([0-9A-Z]{4})", raw.strip().upper())
        return m.group(1).upper() if m else ""
    if v == "hp":
        return (inv.get("board", {}).get("product") or "").strip().upper()
    name = (inv.get("model") or "Modelo").strip()
    return re.sub(r'[<>:"/\\|?*]+', "_", name)


def packs_root() -> Path:
    return app_dir() / PACKS_DIR_NAME


def pack_dir(inv: dict) -> Path:
    return packs_root() / VENDOR_DIR.get(inv.get("vendor", "other"), "Outros") / (model_id(inv) or "desconhecido")


def local_status(inv: dict) -> dict:
    d = pack_dir(inv)
    infs = list(d.rglob("*.inf")) if d.is_dir() else []
    size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0
    info = ""
    meta = d / "pacote.txt"
    if meta.exists():
        info = meta.read_text(encoding="utf-8", errors="ignore").strip()
    return {"vendor": inv.get("vendor"), "model_id": model_id(inv), "path": str(d), "root": str(packs_root()),
            "available": bool(infs), "drivers": len(infs), "size": size, "info": info,
            "downloadable": inv.get("vendor") in CATALOGS}


# --------------------------------------------------------------------------- rede
def _download(url: str, dest: Path, progress: Progress, label: str, cancel: threading.Event) -> None:
    from .updater import _opener, _ps   # mesma rede da atualização (proxy do Windows como plano B)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _opener().open(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, 0.0
            while True:
                if cancel.is_set():
                    raise RuntimeError("Cancelado")
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total and time.time() - last > 0.5:
                    last = time.time()
                    progress(5 + 75 * done / total, f"{label}: {done / 1e6:.0f} de {total / 1e6:.0f} MB")
    except RuntimeError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:
        log(f"Download pelo Python falhou ({e}); tentando pelo Windows…", "warn")
        tmp.unlink(missing_ok=True)
        progress(-1, f"{label} (pelo proxy do Windows)…")
        _ps(f"$wc = New-Object Net.WebClient; $wc.Proxy = $proxy; $wc.Headers['User-Agent'] = '{UA}'; "
            f"$wc.DownloadFile('{url}', '{tmp}')", 4 * 3600)
    dest.unlink(missing_ok=True)
    tmp.rename(dest)


def _expand_cab(cab: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    r = run(["expand.exe", str(cab), "-F:*", str(out)], timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"expand falhou: {(r.stdout + r.stderr).strip()[-200:]}")


def _catalog_xml(vendor: str, progress: Progress, cancel: threading.Event) -> ET.Element:
    work = sub_work_dir("catalogos")
    url = CATALOGS[vendor]
    progress(2, "Baixando o catálogo de drivers do fabricante…")
    raw = work / Path(url).name
    _download(url, raw, lambda p, t: None, "Catálogo", cancel)
    if raw.suffix.lower() == ".cab":
        out = work / (vendor + "-catalogo")
        shutil.rmtree(out, ignore_errors=True)
        _expand_cab(raw, out)
        xml = next(out.glob("*.xml"))
    else:
        xml = raw
    return ET.parse(xml).getroot()


def _strip(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _iter(el: ET.Element, name: str):
    for x in el.iter():
        if _strip(x.tag) == name:
            yield x


def _windows_label() -> tuple[str, str]:
    """('11'|'10', '23H2' ...) da máquina atual."""
    import platform
    build = int(platform.version().split(".")[-1]) if IS_WIN else 22631
    major = "11" if build >= 22000 else "10"
    disp = ""
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
                disp = winreg.QueryValueEx(k, "DisplayVersion")[0]
        except OSError:
            pass
    return major, disp


def find_pack(inv: dict, progress: Progress, cancel: threading.Event) -> dict:
    """Procura no catálogo oficial o pacote certo para o modelo. Retorna {url, name, size, sha256, md5}."""
    vendor, mid = inv.get("vendor"), model_id(inv)
    if vendor not in CATALOGS:
        raise RuntimeError("Este fabricante não publica pacotes de drivers: coloque os drivers na pasta manualmente.")
    if not mid:
        raise RuntimeError("Não consegui identificar o código do modelo desta máquina.")
    win, disp = _windows_label()
    root = _catalog_xml(vendor, progress, cancel)
    progress(4, f"Procurando o pacote do modelo {mid}…")
    cands = []
    if vendor == "dell":
        base = "https://downloads.dell.com/"
        for pkg in _iter(root, "DriverPackage"):
            ids = {m.get("systemID", "").upper() for m in _iter(pkg, "Model")}
            oss = " ".join(o.get("osCode", "") for o in _iter(pkg, "OperatingSystem"))
            if mid in ids and "winpe" not in oss.lower() and ("Windows" + win) in oss:
                cands.append({"url": base + pkg.get("path", ""), "name": Path(pkg.get("path", "")).name,
                              "size": int(pkg.get("size") or 0), "md5": (pkg.get("hashMD5") or "").lower(),
                              "date": pkg.get("dateTime", ""), "ver": pkg.get("dellVersion", "")})
        cands.sort(key=lambda c: c["date"], reverse=True)
    elif vendor == "lenovo":
        for model in _iter(root, "Model"):
            types = []
            for t in _iter(model, "Type"):
                types += [x.strip().upper() for x in (t.text or "").split(",")]
            if mid not in types:
                continue
            for sccm in _iter(model, "SCCM"):
                if sccm.get("os") == f"win{win}" and sccm.text:
                    cands.append({"url": sccm.text.strip(), "name": Path(sccm.text.strip()).name,
                                  "size": 0, "ver": sccm.get("version", ""), "model": model.get("name", "")})
        # versão do Windows igual primeiro, depois a mais nova
        cands.sort(key=lambda c: (c["ver"] == disp, c["ver"]), reverse=True)
    elif vendor == "hp":
        spq = {}
        for sp in _iter(root, "SoftPaq"):
            d = {_strip(c.tag): (c.text or "").strip() for c in sp}
            if d.get("Id"):
                spq[d["Id"]] = d
        for p in _iter(root, "ProductOSDriverPack"):
            d = {_strip(c.tag): (c.text or "").strip() for c in p}
            ids = [x.strip().upper() for x in d.get("SystemId", "").split(",")]
            if mid in ids and f"Windows {win}" in d.get("OSName", "") and d.get("SoftPaqId") in spq:
                s = spq[d["SoftPaqId"]]
                cands.append({"url": s.get("Url", "").replace("ftp://", "https://"), "name": Path(s.get("Url", "")).name,
                              "size": int(s.get("Size") or 0), "sha256": s.get("SHA256", "").lower(),
                              "md5": s.get("MD5", "").lower(), "ver": d.get("OSName", ""),
                              "date": s.get("DateReleased", "")})
        cands.sort(key=lambda c: (disp in c["ver"], c["ver"], c["date"]), reverse=True)
    if not cands:
        raise RuntimeError(f"O catálogo do fabricante não tem pacote de drivers do Windows {win} para o modelo {mid}.")
    return cands[0]


# --------------------------------------------------------------------------- baixar e extrair
def download_pack(inv: dict, progress: Progress, cancel: threading.Event) -> str:
    pack = find_pack(inv, progress, cancel)
    dest = pack_dir(inv)
    work = sub_work_dir("pacotes")
    file = work / pack["name"]
    size = f" ({pack['size'] / 1e9:.1f} GB)" if pack.get("size") else ""
    log(f"Pacote encontrado: {pack['name']}{size}. Baixando…")
    _download(pack["url"], file, progress, "Baixando pacote", cancel)
    progress(82, "Conferindo o arquivo…")
    want_sha, want_md5 = pack.get("sha256"), pack.get("md5")
    if want_sha or want_md5:
        h = hashlib.sha256() if want_sha else hashlib.md5()
        with open(file, "rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest().lower() != (want_sha or want_md5):
            file.unlink(missing_ok=True)
            raise RuntimeError("O pacote baixado não confere com o hash do catálogo do fabricante.")
    progress(85, "Extraindo os drivers (pode levar alguns minutos)…")
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    vendor = inv.get("vendor")
    ext = file.suffix.lower()
    if ext == ".cab":
        _expand_cab(file, dest)
    elif vendor == "dell":
        run([str(file), "/s", f"/e={dest}"], timeout=3600)
    elif vendor == "hp":
        run([str(file), "/s", "/e", "/f", str(dest)], timeout=3600)
    elif vendor == "lenovo":
        run([str(file), "/VERYSILENT", "/SUPPRESSMSGBOXES", f"/DIR={dest}", "/EXTRACT=YES"], timeout=3600)
    infs = list(dest.rglob("*.inf"))
    if not infs:
        raise RuntimeError("O pacote foi baixado, mas nenhum driver (.inf) apareceu depois de extrair.")
    (dest / "pacote.txt").write_text(
        f"{pack['name']}\nVersão/SO: {pack.get('ver', '')}\nBaixado em {time.strftime('%d/%m/%Y %H:%M')}\n"
        f"Modelo: {inv.get('manufacturer', '')} {inv.get('model', '')} ({model_id(inv)})\n", encoding="utf-8")
    file.unlink(missing_ok=True)
    progress(100, f"{len(infs)} drivers prontos em {dest}")
    log(f"Pacote offline salvo em {dest} ({len(infs)} drivers).", "ok")
    return f"{len(infs)} drivers salvos na pasta '{PACKS_DIR_NAME}'."


# --------------------------------------------------------------------------- instalar (sem internet)
def install_pack(inv: dict, progress: Progress, cancel: threading.Event) -> str:
    d = pack_dir(inv)
    infs = sorted(d.rglob("*.inf")) if d.is_dir() else []
    if not infs:
        raise FileNotFoundError(f"Nenhum pacote para este modelo em {d}.")
    total = len(infs)
    log(f"Instalando pacote offline: {total} drivers de {d}.")
    done = [0]
    ok = [0]

    def on_line(line: str):
        low = line.lower()
        if "adding driver package" in low or "adicionando o pacote de driver" in low or ".inf" in low and ":" not in line[:3]:
            done[0] += 1
            progress(min(99.0, 100 * done[0] / max(total, 1)), f"Driver {min(done[0], total)}/{total}")
        if "successfully" in low or "êxito" in low or "sucesso" in low:
            ok[0] += 1

    progress(1, f"Instalando {total} drivers…")
    rc = stream_cmd(["pnputil", "/add-driver", str(d / "*.inf"), "/subdirs", "/install"], on_line,
                    timeout=3 * 3600, cancel=cancel)
    if cancel.is_set():
        raise RuntimeError("Cancelado")
    progress(100, "Pacote offline instalado")
    msg = f"Pacote offline: {total} drivers processados"
    if rc == 3010:
        msg += " (reinicialização recomendada)"
    elif rc not in (0, 259):   # 259 = nenhum driver novo aplicável
        log(f"pnputil terminou com código {rc}.", "warn")
    log(msg + ".", "ok")
    return msg + "."
