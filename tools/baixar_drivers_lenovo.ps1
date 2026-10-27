# Baixa do catálogo OFICIAL da Lenovo os drivers de Wi-Fi mais recentes (só Wi-Fi, sem rede cabeada) e os extrai
# em Recursos\Drivers Lenovo\<família>\ (arquivos .inf, instalados depois com pnputil).
# Roda no GitHub Actions ao gerar o .exe, então cada versão do programa sai com os drivers mais novos.
#
#   pwsh tools/baixar_drivers_lenovo.ps1 [-Destino 'Recursos/Drivers Lenovo']
param([string]$Destino = 'Recursos/Drivers Lenovo')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072

# Modelos de referência (4 primeiros dígitos do Machine Type). Cobrem os chips de rede que a Lenovo usa:
# Intel, Realtek, MediaTek e Qualcomm (Wi-Fi) e Realtek / Intel (cabo).
$modelos = @(
    '21JK','21JR','21M7','21MA','21E6','21ED',   # ThinkPad E14/E16 (Intel e AMD)
    '21HD','21HF','21ML','21MC',                 # ThinkPad T14 G4/G5
    '21H1','21H3','21L1',                        # ThinkPad L14/L15
    '21HM','21KC',                               # ThinkPad X1 Carbon / X13
    '21KG','21KJ','21DJ','21CX','21MR','21MV',   # ThinkBook 14/15/16
    '83A0','83A1','82TT','82UT','83FR',          # Lenovo V14/V15
    '12JF','12LL','11T3','12E3','12SN',          # ThinkCentre neo / M70q / M70s
    '20T6','20TD','20X1','20VE','21A0',          # ThinkPad E14/E15 G2, L14 G2, ThinkBook G2 (Realtek LAN)
    '82KB','82TS','82NV','82QY',                 # Lenovo V14/V15 G2/G3
    '11MY','10T7','11A4','11SE','11EX','11DF',   # ThinkCentre M70q G2, M720q, M75q, neo 50t, M70s, M80s
    '30GS','30GT','30FM','30E0','30DH'           # ThinkStation P3 / P360 / P350 (Realtek Ethernet)
)
# Categorias do catálogo que interessam (texto do catálogo da Lenovo)
$cats = 'Wireless LAN|WLAN|Wi-?Fi|Networking'

$work = Join-Path ([IO.Path]::GetTempPath()) 'lenovo-rede'
New-Item -ItemType Directory -Force -Path $work | Out-Null
New-Item -ItemType Directory -Force -Path $Destino | Out-Null
$wc = New-Object Net.WebClient
$wc.Headers['User-Agent'] = 'PhaelTechProvision-Build'

function Get-Xml($url) {
    try { [xml]$wc.DownloadString($url) } catch { $null }
}
function Familia($titulo) {
    # "Intel Wireless LAN Driver for Windows 11 ..." -> "Intel Wireless LAN Driver"
    $t = ($titulo -split ' for Windows| - | \(Version| for ')[0].Trim()
    $t = $t -replace '[^\w\s\.-]', '' -replace '\s+', ' '
    return $t
}

# 1) junta os pacotes de rede de todos os modelos
$achados = @{}
$global:modelosOk = @()
foreach ($m in $modelos) {
    foreach ($so in 'Win11', 'Win10') {
        $cat = Get-Xml "https://download.lenovo.com/catalog/${m}_$so.xml"
        if (-not $cat) { continue }
        foreach ($p in $cat.packages.package) {
            if ([string]$p.category -notmatch $cats) { continue }
            $loc = [string]$p.location
            if ($achados.ContainsKey($loc)) { continue }
            $achados[$loc] = $m
        }
        $global:modelosOk += "$m/$so"
        break
    }
}
Write-Host ("{0} pacote(s) de rede encontrados nos catalogos de {1} modelo(s): {2}" -f $achados.Count, $modelosOk.Count, ($modelosOk -join ' '))
if ($achados.Count -eq 0) { throw 'Nenhum pacote de rede encontrado no catalogo da Lenovo.' }

# 2) lê cada pacote e fica só com o mais novo de cada família (ex.: um Intel Wi-Fi, um Realtek LAN…)
$pacotes = @()
foreach ($loc in $achados.Keys) {
    $x = Get-Xml $loc
    if (-not $x) { continue }
    $pk = $x.Package
    $d = @($pk.Title.Desc)[0]
    $titulo = if ($d -is [string]) { $d } elseif ($d) { [string]$d.'#text' } else { '' }
    if (-not $titulo) { $titulo = [string]$pk.name }
    $arq = [string]$pk.Files.Installer.File.Name
    $extr = [string]$pk.ExtractCommand
    # só Wi-Fi: título precisa falar de rede sem fio, e nada de cabo, celular (WWAN), Bluetooth ou dock
    if (-not $arq -or $titulo -notmatch 'Wireless|WLAN|Wi-?Fi|FastConnect' -or
        $titulo -match 'WWAN|Wireless WAN|5G|LTE|Bluetooth|Firmware|Utility|Dock|Ethernet|Onboard LAN|PRO1000') { continue }
    $data = [string]$pk.ReleaseDate
    $pacotes += [pscustomobject]@{
        Familia = Familia $titulo; Titulo = $titulo; Versao = [string]$pk.version; Data = $data
        Base = $loc.Substring(0, $loc.LastIndexOf('/') + 1); Arquivo = $arq; Extrair = $extr
        Tamanho = [int64]$pk.Files.Installer.File.Size; Sha = [string]$pk.Files.Installer.File.CRC
    }
}
$escolhidos = $pacotes | Group-Object Familia | ForEach-Object {
    $_.Group | Sort-Object @{ e = { try { [datetime]$_.Data } catch { [datetime]::MinValue } } }, Versao -Descending | Select-Object -First 1
}
Write-Host ("{0} familia(s) de driver de rede:" -f @($escolhidos).Count)
$escolhidos | ForEach-Object { Write-Host ("  - {0} {1} ({2}) {3}" -f $_.Familia, $_.Versao, $_.Data, $_.Arquivo) }

# 3) baixa e extrai cada pacote numa área temporária
$stage = Join-Path $work 'extraidos'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
foreach ($e in $escolhidos) {
    $exe = Join-Path $work $e.Arquivo
    try { $wc.DownloadFile($e.Base + $e.Arquivo, $exe) }
    catch { Write-Warning "Falha ao baixar $($e.Arquivo): $($_.Exception.Message)"; continue }
    $tmp = Join-Path $stage ([IO.Path]::GetFileNameWithoutExtension($e.Arquivo))
    # Os pacotes da Lenovo são Inno Setup: /EXTRACT=YES só extrai, sem instalar nada no servidor de build
    $p = Start-Process -FilePath $exe -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$tmp`"", '/EXTRACT="YES"' -Wait -PassThru
    if (-not (Get-ChildItem $tmp -Recurse -Filter *.inf -ErrorAction SilentlyContinue)) {
        Write-Warning "$($e.Arquivo): nenhum .inf depois de extrair (codigo $($p.ExitCode)); ignorado."
    }
}

# 4) de cada driver (.inf com o mesmo nome em vários pacotes) fica SÓ a versão mais nova, e só a pasta
#    do driver (sem instaladores, manuais e cópias repetidas): de ~800 MB para algumas dezenas de MB
function DriverVer($inf) {
    $l = Select-String -Path $inf -Pattern '^\s*DriverVer\s*=\s*([^,;]+)\s*,\s*([0-9\.]+)' | Select-Object -First 1
    if (-not $l) { return [pscustomobject]@{ Data = [datetime]::MinValue; Versao = [version]'0.0' } }
    $d = [datetime]::MinValue; [void][datetime]::TryParse($l.Matches[0].Groups[1].Value.Trim(), [Globalization.CultureInfo]::InvariantCulture, 'None', [ref]$d)
    $v = [version]'0.0'; [void][version]::TryParse($l.Matches[0].Groups[2].Value.Trim(), [ref]$v)
    [pscustomobject]@{ Data = $d; Versao = $v }
}
$infs = Get-ChildItem $stage -Recurse -Filter *.inf | Where-Object {
    $_.FullName -notmatch '\\(x86|Win32|32bit|Win7|Win8|Win81|ARM64)\\' -and
    # placas de servidor/estação (10-100 Gb, Thunderbolt em rede) não interessam para notebook/desktop
    # placas de rede cabeada (Intel e1x/i2xx, Broadcom, Aquantia, servidor) e Thunderbolt ficam de fora
    $_.BaseName -notmatch '^(e1|e2|b57|bxnd|aqnic|rt6|rtux|rtvn|mlx\d|i40e|ixs|ixt|icea|TbtP2p|TbtHost|amdusb4net|PieExtension)' -and
    (Select-String -Path $_.FullName -Pattern '^\s*Class\s*=\s*Net\s*$' -Quiet)
}
$melhores = $infs | ForEach-Object { $dv = DriverVer $_.FullName; $_ | Add-Member -NotePropertyName Dv -NotePropertyValue $dv -PassThru } |
    Group-Object { $_.Name.ToLower() } | ForEach-Object { $_.Group | Sort-Object { $_.Dv.Versao }, { $_.Dv.Data } -Descending | Select-Object -First 1 }
if (Test-Path $Destino) { Get-ChildItem $Destino -Directory | Remove-Item -Recurse -Force }
$lista = @()
$copiadas = @{}
foreach ($inf in $melhores) {
    $dir = $inf.Directory.FullName
    if ($copiadas.ContainsKey($dir)) { continue }      # pasta com vários .inf: copia uma vez
    $copiadas[$dir] = $true
    $alvo = Join-Path $Destino ([IO.Path]::GetFileNameWithoutExtension($inf.Name))
    New-Item -ItemType Directory -Force -Path $alvo | Out-Null
    # a pasta do driver inteira (subpastas inclusas, que alguns .inf usam), menos instaladores e documentos
    Get-ChildItem $dir -Recurse -File | Where-Object { $_.Extension -notmatch '^\.(exe|msi|pdf|htm|html|chm|rtf|doc|docx|zip|log)$' } | ForEach-Object {
        $rel = $_.FullName.Substring($dir.Length).TrimStart('\')
        $dst = Join-Path $alvo $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
        Copy-Item $_.FullName $dst -Force
    }
    $mb = [math]::Round(((Get-ChildItem $alvo -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
    $lista += "{0}  versao {1} ({2:dd/MM/yyyy}) - {3} MB" -f $inf.Name, $inf.Dv.Versao, $inf.Dv.Data, $mb
}
$lista | Out-File (Join-Path $Destino 'LEIA-ME.txt') -Encoding utf8
Write-Host "Drivers de rede Lenovo prontos:"
$lista | ForEach-Object { Write-Host "  $_" }
$total = [math]::Round(((Get-ChildItem $Destino -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "Total: $total MB em $Destino"
if (-not $lista) { throw 'Nenhum driver de rede Lenovo foi extraido.' }
