"""Versão e repositório de atualização. O GitHub Actions reescreve VERSION a cada release."""
VERSION = "2.0.5"
# Repositório público de onde o programa baixa as atualizações (dono/nome)
GITHUB_REPO = "rafhaeldellteam-jpg/phael-tech-provision"
# Impressão digital do certificado que assina o .exe (preenchida pelo GitHub Actions quando há certificado).
# Com ela preenchida, o programa só aceita atualizações assinadas por esse mesmo certificado.
SIGN_THUMBPRINT = ""
