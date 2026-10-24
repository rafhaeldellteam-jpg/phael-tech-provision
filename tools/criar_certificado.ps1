# Cria o certificado de assinatura digital da Phael Tech (autoassinado) e prepara os 2 segredos do GitHub.
# Rode UMA vez, no seu computador, pelo criar_certificado.bat. Guarde a pasta gerada em local seguro.
$ErrorActionPreference = 'Stop'
$repo = 'rafhaeldellteam-jpg/phael-tech-provision'
$out = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Certificado Phael Tech (NAO COMPARTILHAR)'
New-Item -ItemType Directory -Path $out -Force | Out-Null

$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=Phael Tech, O=Phael Tech, C=BR' `
    -FriendlyName 'Phael Tech - Assinatura de codigo' -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 `
    -KeyExportPolicy Exportable -CertStoreLocation 'Cert:\CurrentUser\My' -NotAfter (Get-Date).AddYears(10)

$chars = [char[]]((48..57) + (65..90) + (97..122))
$senha = -join (1..32 | ForEach-Object { $chars | Get-Random })
$sec = ConvertTo-SecureString $senha -AsPlainText -Force
Export-PfxCertificate -Cert $cert -FilePath (Join-Path $out 'phaeltech.pfx') -Password $sec | Out-Null
Export-Certificate -Cert $cert -FilePath (Join-Path $out 'phaeltech.cer') | Out-Null
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $out 'phaeltech.pfx')))
[IO.File]::WriteAllText((Join-Path $out '1 - SIGN_PFX_BASE64.txt'), $b64)
[IO.File]::WriteAllText((Join-Path $out '2 - SIGN_PFX_PASSWORD.txt'), $senha)
@"
COMO ATIVAR A ASSINATURA DIGITAL DO PHAEL TECH PROVISION
=======================================================
1. No GitHub, abra:  https://github.com/$repo/settings/secrets/actions/new
2. Name: SIGN_PFX_BASE64   ->  Secret: cole TODO o conteúdo do arquivo "1 - SIGN_PFX_BASE64.txt"  ->  Add secret
3. Clique em "New repository secret" de novo
   Name: SIGN_PFX_PASSWORD ->  Secret: cole o conteúdo do arquivo "2 - SIGN_PFX_PASSWORD.txt" ->  Add secret
4. Pronto: a próxima versão publicada já sai assinada como "Phael Tech".

Impressão digital deste certificado: $($cert.Thumbprint)

IMPORTANTE
- Guarde esta pasta num local seguro (ex.: pendrive pessoal). Quem tiver o .pfx e a senha consegue assinar em nome da Phael Tech.
- Depois de cadastrar os segredos no GitHub, você pode apagar os dois .txt.
- Não envie estes arquivos por chat, e-mail ou para o repositório.
"@ | Set-Content -Path (Join-Path $out 'LEIA-ME.txt') -Encoding UTF8

Start-Process "https://github.com/$repo/settings/secrets/actions/new"
Start-Process explorer.exe $out
Write-Host ""
Write-Host "Certificado criado em: $out" -ForegroundColor Green
Write-Host "Siga o LEIA-ME.txt para cadastrar os 2 segredos no GitHub." -ForegroundColor Green
