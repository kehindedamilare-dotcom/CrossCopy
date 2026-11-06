# -*- mode: python ; coding: utf-8 -*-
"""Release build: one windowed .exe with the tray assets embedded.

Build with:  .venv\\Scripts\\pyinstaller --noconfirm CrossCopy.spec
"""

a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('assets/crosscopy.ico', 'assets'),
        ('assets/crosscopy.png', 'assets'),
    ],
    hiddenimports=[
        # pystray picks its backend at runtime, so PyInstaller cannot see it.
        'pystray._win32',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest', 'unittest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CrossCopy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/crosscopy.ico'],
)
