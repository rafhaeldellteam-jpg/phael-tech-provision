# Baixa do catálogo OFICIAL da Lenovo o instalador do driver de Wi-Fi Intel mais recente e o guarda,
# compactado, em Recursos\Drivers Lenovo (cerca de 20 MB). O programa extrai e instala na hora de usar.
# Roda no GitHub Actions ao gerar o .exe, então cada versão sai com o driver mais novo.
#
#   pwsh tools/baixar_drivers_lenovo.ps1 [-Destino 'Recursos/Drivers Lenovo']
param([string]$Destino = 'Recursos/Drivers Lenovo')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072

# Modelos de referência (ThinkPad / ThinkBook / V atuais) — todos usam o pacote Intel Wi-Fi da Lenovo
$modelos = @('21JK','21M7','21HD','21ML','21H1','21L1','21KG','21MR','83A1','21E6','21HM')
$wc = New-Object Net.WebClient
$wc.Headers['User-Agent'] = 'PhaelTechProvision-Build'
function Get-Xml($url) { try { [xml]$wc.DownloadString($url) } catch { $null } }

$vistos = @{}
$pacotes = @()
foreach ($m in $modelos) {
    $cat = Get-Xml "https://download.lenovo.com/catalog/${m}_Win11.xml"
    if (-not $cat) { continue }
    foreach ($p in $cat.packages.package) {
        $loc = [string]$p.location
        if ([string]$p.category -notmatch 'Wireless LAN|Networking' -or $vistos.ContainsKey($loc)) { continue }
        $vistos[$loc] = 1
        $x = Get-Xml $loc
        if (-not $x) { continue }
        $pk = $x.Package
        $d = @($pk.Title.Desc)[0]
        $titulo = if ($d -is [string]) { $d } elseif ($d) { [string]$d.'#text' } else { '' }
        # só o driver Wi-Fi da Intel (pacotes que misturam Realtek/MediaTek ficam de fora)
        if ($titulo -notmatch '^Intel .*(Wireless LAN|WLAN)' -or $titulo -match 'Bluetooth|WWAN') { continue }
        $v = [version]'0.0'
        if (-not [version]::TryParse(([string]$pk.version -split '/')[0], [ref]$v)) { continue }
        $pacotes += [pscustomobject]@{
            Titulo = $titulo; Versao = $v; Data = [string]$pk.ReleaseDate
            Url = $loc.Substring(0, $loc.LastIndexOf('/') + 1) + [string]$pk.Files.Installer.File.Name
            Arquivo = [string]$pk.Files.Installer.File.Name; Tamanho = [int64]$pk.Files.Installer.File.Size
        }
    }
}
if (-not $pacotes) { throw 'Driver Wi-Fi Intel nao encontrado no catalogo da Lenovo.' }
$e = $pacotes | Sort-Object Versao, Data -Descending | Select-Object -First 1
Write-Host ("Escolhido: {0} {1} ({2}) {3} - {4:N1} MB" -f $e.Titulo, $e.Versao, $e.Data, $e.Arquivo, ($e.Tamanho / 1MB))

if (Test-Path $Destino) { Remove-Item $Destino -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Destino | Out-Null
$exe = Join-Path $Destino $e.Arquivo
$wc.DownloadFile($e.Url, $exe)

# confere que o instalador extrai os drivers (.inf) do jeito que o programa vai usar
$tmp = Join-Path ([IO.Path]::GetTempPath()) 'lenovo-wifi-teste'
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Start-Process -FilePath $exe -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$tmp`"", '/EXTRACT="YES"' -Wait
$infs = @(Get-ChildItem $tmp -Recurse -Filter *.inf -ErrorAction SilentlyContinue)
if ($infs.Count -eq 0) { Remove-Item $Destino -Recurse -Force; throw "O instalador $($e.Arquivo) nao extraiu nenhum .inf." }
Remove-Item $tmp -Recurse -Force

"{0}`r`nVersao {1} - {2}`r`nPacote Lenovo {3} ({4:N1} MB)" -f $e.Titulo, $e.Versao, $e.Data, $e.Arquivo, ((Get-Item $exe).Length / 1MB) |
    Out-File (Join-Path $Destino 'LEIA-ME.txt') -Encoding utf8
Write-Host "Pronto: $exe ($($infs.Count) .inf dentro)"
