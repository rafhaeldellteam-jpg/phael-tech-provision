"""Inventário de hardware, Service Tag por fabricante, saúde e métricas ao vivo."""
from __future__ import annotations

import json
import time

try:
    import psutil  # métricas ao vivo sem abrir PowerShell a cada segundo
except ImportError:  # pragma: no cover
    psutil = None

from .util import IS_WIN, run_ps

INVENTORY_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
function Clean([object]$v) { if ($null -eq $v) { return '' } ; return ([string]$v).Trim() }
$junk = '^(none|null|n/?a|default string|to be filled.*|o\.?e\.?m\.?|system serial number|system product name|chassis serial number|base board serial number|serial number|0+|1234567890|0123456789|123456789|x+|invalid|not specified|not applicable|unknown|\.+|-+)$'
function Good([object]$v) { $s = Clean $v ; return ($s -ne '' -and ($s.ToLower() -notmatch $junk)) }

$cs   = Get-CimInstance Win32_ComputerSystem
$csp  = Get-CimInstance Win32_ComputerSystemProduct
$bios = Get-CimInstance Win32_BIOS
$bb   = Get-CimInstance Win32_BaseBoard | Select-Object -First 1
$enc  = Get-CimInstance Win32_SystemEnclosure | Select-Object -First 1
$os   = Get-CimInstance Win32_OperatingSystem
$cpus = @(Get-CimInstance Win32_Processor)
$cpu  = $cpus | Select-Object -First 1

# ---- fabricante normalizado
$mfgAll = ((Clean $cs.Manufacturer), (Clean $csp.Vendor), (Clean $bb.Manufacturer), (Clean $bios.Manufacturer)) -join ' | '
$vendor = 'other'
if     ($mfgAll -match 'Dell')                 { $vendor = 'dell' }
elseif ($mfgAll -match 'Lenovo')               { $vendor = 'lenovo' }
elseif ($mfgAll -match 'HP|Hewlett')           { $vendor = 'hp' }
elseif ($mfgAll -match 'Positivo|Vaio|Quantum') { $vendor = 'positivo' }

# ---- modelo amigável (Lenovo guarda o nome comercial em Version)
$model = Clean $cs.Model
if ($vendor -eq 'lenovo' -and (Good $csp.Version)) { $model = (Clean $csp.Version) + ' (' + (Clean $cs.Model) + ')' }
$sku = Clean $cs.SystemSKUNumber

# ---- Service Tag / número de série: BIOS > gabinete > produto > placa-mãe
$serial = ''
$serialSrc = ''
foreach ($pair in @(@($bios.SerialNumber,'BIOS'), @($enc.SerialNumber,'Gabinete'), @($csp.IdentifyingNumber,'Produto'), @($bb.SerialNumber,'Placa-mãe'))) {
    if (Good $pair[0]) { $serial = Clean $pair[0] ; $serialSrc = $pair[1] ; break }
}
$asset = '' ; if (Good $enc.SMBIOSAssetTag) { $asset = Clean $enc.SMBIOSAssetTag }

# ---- memória
$sticks = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object {
    $t = [int]$_.SMBIOSMemoryType
    $type = switch ($t) { 20 {'DDR'} 21 {'DDR2'} 24 {'DDR3'} 26 {'DDR4'} 27 {'LPDDR'} 28 {'LPDDR2'} 29 {'LPDDR3'} 30 {'LPDDR4'} 34 {'DDR5'} 35 {'LPDDR5'} default {''} }
    $ff = switch ([int]$_.FormFactor) { 8 {'DIMM'} 12 {'SODIMM'} 0 {''} default {''} }
    [pscustomobject]@{
        slot = (Clean $_.DeviceLocator); bank = (Clean $_.BankLabel)
        size = [double]$_.Capacity
        speed = if ($_.ConfiguredClockSpeed) { [int]$_.ConfiguredClockSpeed } else { [int]$_.Speed }
        maker = (Clean $_.Manufacturer); part = (Clean $_.PartNumber)
        type = $type; form = $ff
    }
})
$slots = 0
Get-CimInstance Win32_PhysicalMemoryArray | ForEach-Object { if ($_.Use -eq 3 -or $_.Use -eq $null) { $slots += [int]$_.MemoryDevices } }
if ($slots -lt $sticks.Count) { $slots = $sticks.Count }

# ---- GPU (memória real via registro: AdapterRAM do WMI trava em 4 GB)
$gpuMem = @{}
Get-ItemProperty 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' | ForEach-Object {
    $q = $_.'HardwareInformation.qwMemorySize'
    if ($q -and $_.DriverDesc) { $gpuMem[$_.DriverDesc] = [double]$q }
}
$gpus = @(Get-CimInstance Win32_VideoController | Where-Object { $_.Name } | ForEach-Object {
    $mem = if ($gpuMem.ContainsKey($_.Name)) { $gpuMem[$_.Name] } else { [double]$_.AdapterRAM }
    [pscustomobject]@{ name = (Clean $_.Name); driver = (Clean $_.DriverVersion); mem = $mem; status = (Clean $_.Status) }
})

# ---- áudio
$audio = @(Get-CimInstance Win32_SoundDevice | Where-Object { $_.Name } | ForEach-Object {
    [pscustomobject]@{ name = (Clean $_.Name); maker = (Clean $_.Manufacturer); status = (Clean $_.Status) }
} | Sort-Object name -Unique)

# ---- rede
$nics = @(Get-CimInstance Win32_NetworkAdapter | Where-Object { $_.PhysicalAdapter -eq $true -and $_.Name -notmatch 'Virtual|Hyper-V|VPN|Bluetooth|WAN Miniport' } | ForEach-Object {
    $wifi = ($_.Name -match 'Wi-?Fi|Wireless|802\.11|WLAN' -or $_.NetConnectionID -match 'Wi-?Fi|Wireless')
    [pscustomobject]@{ name = (Clean $_.Name); kind = if ($wifi) { 'Wi-Fi' } else { 'Ethernet' }; mac = (Clean $_.MACAddress); up = ($_.NetConnectionStatus -eq 2) }
})

# ---- discos
$disks = @(Get-PhysicalDisk | ForEach-Object {
    $rc = $_ | Get-StorageReliabilityCounter
    [pscustomobject]@{
        model = (Clean $_.FriendlyName); media = [string]$_.MediaType; bus = [string]$_.BusType
        size = [double]$_.Size; health = [string]$_.HealthStatus
        temp = if ($rc -and $rc.Temperature) { [int]$rc.Temperature } else { $null }
        wear = if ($rc -and $rc.Wear -ne $null) { [int]$rc.Wear } else { $null }
        hours = if ($rc -and $rc.PowerOnHours) { [int]$rc.PowerOnHours } else { $null }
    }
})
if ($disks.Count -eq 0) {
    $disks = @(Get-CimInstance Win32_DiskDrive | ForEach-Object {
        [pscustomobject]@{ model = (Clean $_.Model); media = ''; bus = (Clean $_.InterfaceType); size = [double]$_.Size; health = if ($_.Status -eq 'OK') { 'Healthy' } else { [string]$_.Status }; temp = $null; wear = $null; hours = $null }
    })
}
$volumes = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    [pscustomobject]@{ letter = $_.DeviceID; label = (Clean $_.VolumeName); total = [double]$_.Size; free = [double]$_.FreeSpace }
})

# ---- bateria (notebooks)
$battery = $null
$b = Get-CimInstance Win32_Battery | Select-Object -First 1
if ($b) {
    $design = (Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData | Select-Object -First 1).DesignedCapacity
    $full   = (Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity | Select-Object -First 1).FullChargedCapacity
    $battery = [pscustomobject]@{
        charge = [int]$b.EstimatedChargeRemaining
        design = [double]$design; full = [double]$full
        health = if ($design -gt 0 -and $full -gt 0) { [math]::Round(100.0 * $full / $design, 0) } else { $null }
        name = (Clean $b.Name)
    }
}

# ---- dispositivos com problema (driver ausente / erro)
$problems = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.ConfigManagerErrorCode -ne 0 -and $_.ConfigManagerErrorCode -ne $null } | ForEach-Object {
    [pscustomobject]@{ name = if ($_.Name) { (Clean $_.Name) } else { (Clean $_.DeviceID) }; code = [int]$_.ConfigManagerErrorCode; id = (Clean $_.DeviceID) }
} | Select-Object -First 40)

# ---- segurança / firmware
$secureBoot = $null ; try { $secureBoot = [bool](Confirm-SecureBootUEFI -ErrorAction Stop) } catch {}
$tpm = $null ; try { $t = Get-CimInstance -Namespace root/cimv2/Security/MicrosoftTpm -ClassName Win32_Tpm ; if ($t) { $tpm = (Clean $t.SpecVersion).Split(',')[0] } } catch {}
$fastStartup = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled).HiberbootEnabled
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'

[pscustomobject]@{
    hostname = $env:COMPUTERNAME
    user = $env:USERNAME
    domain = if ($cs.PartOfDomain) { Clean $cs.Domain } else { 'Grupo de trabalho: ' + (Clean $cs.Workgroup) }
    vendor = $vendor
    manufacturer = Clean $cs.Manufacturer
    model = $model
    model_raw = Clean $cs.Model
    sku = $sku
    serial = $serial
    serial_source = $serialSrc
    asset_tag = $asset
    bios = [pscustomobject]@{ version = (Clean $bios.SMBIOSBIOSVersion); date = if ($bios.ReleaseDate) { $bios.ReleaseDate.ToString('dd/MM/yyyy') } else { '' }; maker = (Clean $bios.Manufacturer) }
    board = [pscustomobject]@{ maker = (Clean $bb.Manufacturer); product = (Clean $bb.Product); version = (Clean $bb.Version); serial = (Clean $bb.SerialNumber) }
    cpu = [pscustomobject]@{
        name = (Clean $cpu.Name); cores = [int](($cpus | Measure-Object NumberOfCores -Sum).Sum)
        threads = [int](($cpus | Measure-Object NumberOfLogicalProcessors -Sum).Sum)
        clock = [int]$cpu.MaxClockSpeed; socket = (Clean $cpu.SocketDesignation); count = $cpus.Count
        status = (Clean $cpu.Status)
    }
    ram = [pscustomobject]@{ total = [double]$cs.TotalPhysicalMemory; installed = [double](($sticks | Measure-Object size -Sum).Sum); slots = $slots; sticks = $sticks }
    gpus = $gpus
    audio = $audio
    nics = $nics
    disks = $disks
    volumes = $volumes
    battery = $battery
    problems = $problems
    secure_boot = $secureBoot
    tpm = $tpm
    fast_startup = if ($fastStartup -eq $null) { $null } else { [int]$fastStartup }
    os = [pscustomobject]@{
        caption = (Clean $os.Caption); version = (Clean $os.Version); build = (Clean $cv.CurrentBuild) + '.' + [string]$cv.UBR
        display = (Clean $cv.DisplayVersion); arch = (Clean $os.OSArchitecture)
        installed = if ($os.InstallDate) { $os.InstallDate.ToString('dd/MM/yyyy') } else { '' }
        boot = if ($os.LastBootUpTime) { $os.LastBootUpTime.ToString('o') } else { '' }
        uptime = if ($os.LastBootUpTime) { [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds } else { 0 }
    }
} | ConvertTo-Json -Compress -Depth 6
"""

VENDOR_LABEL = {"dell": "Dell", "lenovo": "Lenovo", "hp": "HP", "positivo": "Positivo", "other": "Outro"}
SERIAL_LABEL = {"dell": "Service Tag", "lenovo": "Número de série (S/N)", "hp": "Número de série (S/N)",
                "positivo": "Número de série (S/N)", "other": "Número de série"}
PROBLEM_CODES = {1: "Não configurado", 3: "Driver corrompido", 10: "Não pode iniciar", 18: "Reinstalar driver",
                 22: "Desativado", 24: "Ausente/sem driver", 28: "Driver não instalado", 31: "Erro no driver",
                 43: "Parado pelo Windows", 45: "Desconectado"}


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def dell_express_code(tag: str) -> str:
    """Express Service Code da Dell = Service Tag (base 36) convertida para decimal."""
    try:
        return str(int(tag, 36)) if tag and len(tag) == 7 and tag.isalnum() else ""
    except ValueError:
        return ""


def support_url(vendor: str, serial: str) -> str:
    s = serial or ""
    if vendor == "dell" and s:
        return f"https://www.dell.com/support/home/pt-br/product-support/servicetag/{s}/drivers"
    if vendor == "lenovo" and s:
        return f"https://pcsupport.lenovo.com/br/pt/search?query={s}"
    if vendor == "hp":
        return f"https://support.hp.com/br-pt/drivers?serialnumber={s}" if s else "https://support.hp.com/br-pt/drivers"
    if vendor == "positivo":
        return "https://www.positivotecnologia.com.br/para-voce/drivers"
    return ""


def collect_inventory() -> dict:
    if not IS_WIN:
        raise RuntimeError("Inventário disponível apenas no Windows.")
    r = run_ps(INVENTORY_PS, timeout=180)
    out = (r.stdout or "").strip()
    if not out:
        raise RuntimeError((r.stderr or "").strip() or "Falha ao consultar o hardware.")
    start = out.find("{")
    data = json.loads(out[start:])
    for key in ("gpus", "audio", "nics", "disks", "volumes", "problems"):
        data[key] = _as_list(data.get(key))
    data["ram"]["sticks"] = _as_list(data["ram"].get("sticks"))
    v = data.get("vendor") or "other"
    data["vendor_label"] = VENDOR_LABEL.get(v, "Outro")
    data["serial_label"] = SERIAL_LABEL.get(v, "Número de série")
    data["express_code"] = dell_express_code(data.get("serial", "")) if v == "dell" else ""
    data["support_url"] = support_url(v, data.get("serial", ""))
    for p in data["problems"]:
        p["reason"] = PROBLEM_CODES.get(p.get("code"), f"Código {p.get('code')}")
    data["health"] = evaluate_health(data)
    return data


# --------------------------------------------------------------------------- saúde
def evaluate_health(inv: dict) -> dict:
    items: list[dict] = []

    def add(key, label, status, detail):
        items.append({"key": key, "label": label, "status": status, "detail": detail})

    cpu = inv.get("cpu", {})
    add("cpu", "Processador", "ok" if cpu.get("status", "OK") in ("OK", "") else "warn",
        "Funcionando normalmente" if cpu.get("status", "OK") in ("OK", "") else f"Status: {cpu.get('status')}")

    ram = inv.get("ram", {})
    total_gb = (ram.get("total") or 0) / 1024 ** 3
    add("ram", "Memória RAM", "warn" if 0 < total_gb < 7.5 else "ok",
        f"{total_gb:.0f} GB utilizáveis" + (" — abaixo de 8 GB" if 0 < total_gb < 7.5 else ""))

    disk_bad = [d for d in inv.get("disks", []) if str(d.get("health", "")).lower() in ("unhealthy", "pred fail")]
    disk_warn = [d for d in inv.get("disks", []) if str(d.get("health", "")).lower() == "warning"
                 or (d.get("wear") or 0) >= 80]
    if disk_bad:
        add("disk", "Discos (SMART)", "bad", f"{disk_bad[0].get('model')}: em risco")
    elif disk_warn:
        add("disk", "Discos (SMART)", "warn", f"{disk_warn[0].get('model')}: pede atenção")
    else:
        add("disk", "Discos (SMART)", "ok", "Todos saudáveis")

    for vol in inv.get("volumes", []):
        if str(vol.get("letter", "")).upper().startswith("C") and vol.get("total"):
            pct_free = 100 * vol["free"] / vol["total"]
            st = "bad" if pct_free < 10 else "warn" if pct_free < 20 else "ok"
            add("space", "Espaço em C:", st, f"{pct_free:.0f}% livre")

    probs = inv.get("problems", [])
    add("devices", "Drivers", "warn" if probs else "ok",
        f"{len(probs)} dispositivo(s) com problema" if probs else "Nenhum dispositivo com erro")

    bat = inv.get("battery")
    if bat and bat.get("health"):
        h = bat["health"]
        add("battery", "Bateria", "bad" if h < 50 else "warn" if h < 70 else "ok", f"Saúde {h}% da capacidade original")

    up = (inv.get("os") or {}).get("uptime") or 0
    days = up / 86400
    add("uptime", "Tempo ligado", "warn" if days >= 7 else "ok",
        "Sem reiniciar há mais de 7 dias" if days >= 7 else "Reiniciado recentemente")

    bads = sum(1 for i in items if i["status"] == "bad")
    warns = sum(1 for i in items if i["status"] == "warn")
    score = max(0, 100 - bads * 25 - warns * 8)
    if bads:
        title, sub = "Seu PC precisa de atenção", "Um ou mais componentes estão em risco."
    elif warns:
        title, sub = "Seu PC está com pontos de atenção", "Alguns itens pedem acompanhamento."
    else:
        title, sub = "Seu PC está saudável", "Todos os sistemas dentro dos parâmetros normais."
    return {"score": score, "title": title, "subtitle": sub, "bads": bads, "warns": warns, "items": items}


# --------------------------------------------------------------------------- ao vivo
def _power() -> dict | None:
    """Carregador ligado ou não (só notebooks). None = desktop/sem bateria."""
    try:
        b = psutil.sensors_battery()
    except Exception:
        return None
    if b is None:
        return None
    secs = b.secsleft if isinstance(b.secsleft, int) and b.secsleft > 0 else None
    return {"plugged": bool(b.power_plugged), "percent": round(b.percent), "secsleft": secs}


def live_metrics() -> dict:
    if psutil is None:
        return {}
    vm = psutil.virtual_memory()
    try:
        boot = psutil.boot_time()
    except Exception:
        boot = time.time()
    vols = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            vols.append({"letter": part.device.rstrip("\\"), "total": u.total, "free": u.free, "pct": u.percent})
        except Exception:
            continue
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "mem_pct": vm.percent, "mem_used": vm.used, "mem_total": vm.total,
        "uptime": int(time.time() - boot),
        "power": _power(),
        "volumes": vols,
    }
