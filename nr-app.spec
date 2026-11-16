# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/Users/a7925/WorkBuddy/2026-11-06-16-54-08/app/nr_app.py'],
    pathex=['C:/Users/a7925/WorkBuddy/2026-11-06-16-54-08/tools', 'C:/Users/a7925/WorkBuddy/2026-11-06-16-54-08/app'],
    binaries=[],
    datas=[('C:/Users/a7925/WorkBuddy/2026-11-06-16-54-08/presets', 'presets')],
    hiddenimports=['ve_cli', 'nr_video', '_paths', '_tools', '_media', '_proc'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='nr-app',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:/Users/a7925/WorkBuddy/2026-11-06-16-54-08/build/app.ico'],
    contents_directory='_internal',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='nr-app',
)
