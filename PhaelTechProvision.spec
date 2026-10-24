# -*- mode: python ; coding: utf-8 -*-
# Gera um único PhaelTechProvision.exe com a interface (ui) e os instaladores (Recursos) embutidos.
# Uma pasta "Recursos" colocada ao lado do .exe tem prioridade sobre a embutida (para trocar instaladores sem recompilar).
from PyInstaller.utils.hooks import collect_data_files

datas = [('ui', 'ui'), ('Recursos', 'Recursos')]
datas += collect_data_files('webview')
hidden = ['webview.platforms.edgechromium', 'webview.platforms.winforms', 'clr', 'psutil']

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    excludes=['tkinter', 'customtkinter', 'PIL', 'numpy', 'webview.platforms.qt', 'webview.platforms.gtk',
              'webview.platforms.cocoa', 'webview.platforms.android'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Phael Tech Provision',
    icon='ui/assets/logo.ico',
    console=False,
    uac_admin=True,          # já abre como administrador
    upx=False,
    version='version_info.txt',
)
