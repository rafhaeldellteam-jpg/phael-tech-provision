# Phael Tech Provision 2

Programa de provisionamento para preparar máquinas no modo de auditoria do Sysprep.
Ele foi reescrito do zero: interface nova com modo claro e escuro, detecção automática do fabricante e Service Tag correta.

## Como gerar o .exe

1. Instale o **Python 3.13** (python.org). Durante a instalação, marque "Add python.exe to PATH".
2. Dê dois cliques em **`build.bat`**.
3. O executável sai em **`dist\PhaelTechProvision.exe`**. É um arquivo único, com tudo embutido, que já abre como administrador.

Para testar sem compilar, rode o `build.bat` uma vez e depois use o `executar_teste.bat`.

> A interface usa o **Microsoft Edge WebView2**, que já vem no Windows 11 e no Windows 10 atualizado.
> Se for usar em uma imagem antiga do Windows 10 sem WebView2, coloque o `MicrosoftEdgeWebview2Setup.exe` na pasta `Recursos`.
> O programa instala esse componente sozinho ao abrir.

## O que cada aba faz

| Aba | Função |
|---|---|
| **Provisionamento** | Executa em sequência: desativa a inicialização rápida → Office → aplicativos → Windows Update (com drivers) → drivers do fabricante → finalização. Quando o Windows pede para reiniciar, o programa se agenda, reinicia, reabre sozinho e continua de onde parou. |
| **Hardware** | Nota de saúde, tempo ligado sem reiniciar, fabricante/modelo, **Service Tag / S/N** (com botão copiar e link para o suporte), patrimônio, placa-mãe, BIOS, processador, RAM com **pentes por slot** (tamanho, tipo, velocidade e slots livres), áudio, vídeo, discos (SMART, temperatura e desgaste), placas de rede, bateria, Secure Boot e TPM. |
| **Drivers** | Mostra a marca detectada e os dispositivos sem driver. Busca os drivers do fabricante ou do Windows Update e instala os pacotes offline. |
| **Aplicativos** | Catálogo de instaladores da pasta `Recursos\Programas`. Os aplicativos marcados também entram no provisionamento. |
| **Rede** | Mostra os adaptadores (IP, gateway, DNS, SSID) e testa gateway, internet, DNS e o servidor do Windows Update. |
| **Limpeza** | Limpa temporários, o cache do Windows Update e os componentes antigos (DISM), esvazia a Lixeira, analisa o espaço por pasta e lista os programas que abrem com o Windows. |
| **Reparo do Windows** | Roda DISM CheckHealth, ScanHealth e RestoreHealth e depois o SFC /scannow, mostrando o progresso. |
| **Temperatura** | Mostra CPU, GPU, placa-mãe, memória, discos e ventoinhas em tempo real (LibreHardwareMonitor). |
| **Logs** | Mostra tudo o que foi feito. O log também é gravado em `C:\ProgramData\PhaelTechProvision\logs`. |

## Drivers por fabricante (automático)

| Marca | Ferramenta usada | Observação |
|---|---|---|
| **Dell** | Dell Command \| Update (`dcu-cli`) | Se não estiver instalado, o programa usa o instalador do DCU que estiver em `Recursos\Drivers Dell` (nome contendo "Command" e "Update"). Se não houver, instala pelo winget. |
| **Lenovo** | Catálogo oficial Lenovo pelo módulo **LSUClient** (já embutido) | Não precisa instalar o System Update. Baixa direto de download.lenovo.com. |
| **HP** | **HP Image Assistant** | Usa `Recursos\HPIA\HPImageAssistant.exe` ou o `hp-hpia-*.exe` da pasta `Recursos\Drivers HP`. Se não houver nenhum dos dois, baixa a versão mais nova do site da HP. |
| **Positivo / outras** | Catálogo de drivers do **Windows Update** | A Positivo não tem ferramenta de linha de comando. O botão de suporte abre o site de drivers. |

Se a ferramenta do fabricante falhar, o programa usa automaticamente os drivers do Windows Update.
BIOS e firmware só são instalados se a opção "Incluir BIOS e firmware" estiver marcada.

## Pasta Recursos

```
Recursos\
  OfficeSetup.exe            instalador do Microsoft 365 (requer internet)
  Programas\                 Chrome, AnyDesk, WinRAR, Firefox, Brave, CrystalDiskInfo, CPU-Z, DDU, Lenovo System Update
  Drivers Dell\              pacote offline Dell (Wi-Fi, Bluetooth, Intel HID); pode receber o instalador do Dell Command Update
  Drivers HP\                pacote offline HP; pode receber o hp-hpia-x.y.z.exe
  portcode Lenovo\           Portcode.reg
  LSUClient\                 módulo de drivers Lenovo (licença MIT)
  LibreHardwareMonitorLib.dll, HidSharp.dll   sensores de temperatura
```

Para **trocar ou atualizar um instalador sem recompilar**, coloque uma pasta `Recursos` ao lado do `PhaelTechProvision.exe`.
Os arquivos dessa pasta têm prioridade sobre os que estão embutidos no .exe.

## Correções em relação à versão 1.5

- **Service Tag:** a versão antiga lia primeiro o *Asset Tag* da BIOS, que quase sempre vem vazio ou com texto genérico. Agora a ordem é BIOS, depois gabinete, depois produto e por último placa-mãe, ignorando valores genéricos. A Dell também mostra o Express Service Code.
- **Windows Update:** antes ele abria as Configurações, disparava o UsoClient e usava a API ao mesmo tempo. Agora usa só a API oficial, uma atualização por vez, com progresso real. Inclui drivers e atualizações do Office (Microsoft Update) e tem limite de ciclos para não ficar preso em loop.
- **Continuação após reiniciar:** o estado fica salvo em `C:\ProgramData\PhaelTechProvision\estado.json`. Ao reabrir, a tela mostra o que falta e continua em 10 s.
- **Inicialização rápida:** além de desativar, o programa confere no registro se a alteração foi aplicada e grava a política para o Windows não reativá-la.
- **Office:** detecta quando a instalação realmente terminou (pelo registro do Click-to-Run) e não só quando o instalador fecha. Se o Office já estiver instalado, a etapa é pulada.
- **Sensores:** as leituras alternativas de temperatura (disco e zona ACPI) eram calculadas mas nunca apareciam na tela. Isso foi corrigido.
- **Memória de vídeo:** agora vem do registro, porque o WMI trava em 4 GB.
- **Limpeza:** não apaga mais a pasta temporária do próprio programa enquanto ele está aberto.
- **Teste de rede:** o ping em 127.0.0.1 não testava nada de verdade. Agora o teste usa gateway, internet, DNS e o servidor do Windows Update.

## Estrutura do código

```
app.py              janela e ponte entre a interface e o Python (classe Api)
core\util.py        caminhos, PowerShell, processos, log
core\sysinfo.py     inventário de hardware, Service Tag, saúde
core\provision.py   energia, Office, Windows Update, finalização, continuação após reiniciar
core\drivers.py     Dell / Lenovo / HP / Positivo, Windows Update (drivers), pacotes offline
core\programs.py    catálogo de aplicativos
core\maintenance.py rede, limpeza, reparo (DISM/SFC), sensores
ui\                 interface (index.html, app.css, app.js)
```

## Atualização automática (GitHub)

O programa consulta a última **Release** do repositório configurado em `core/version.py` (`GITHUB_REPO`).
Se houver versão maior que a dele, mostra um aviso, conta 15 s e se atualiza sozinho:
baixa o `PhaelTechProvision.exe` da release, confere o SHA-256, troca o próprio arquivo e reabre.
Ele não atualiza no meio de um provisionamento, e sem internet simplesmente segue na versão atual.
Também dá para clicar no número da versão, no canto inferior esquerdo, para procurar atualização na hora.

**Para publicar uma versão nova:** GitHub → *Releases* → *Draft a new release* → crie a tag (ex.: `v2.0.1`) → *Publish release*.
O GitHub Actions (`.github/workflows/release.yml`) gera o .exe no Windows com essa versão e anexa à release.
Em poucos minutos, todos os programas abertos com internet se atualizam.
