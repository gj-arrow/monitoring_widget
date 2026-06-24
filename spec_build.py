# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file для создания exe приложения."""

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[
        # Добавьте пути к любым дополнительным библиотекам здесь
    ],
    binaries=[],
    datas=[],
    hiddenimports=[
        'psutil',
        'pynvml',
        'wmi',
        'PyQt6.sip',
        'PyQt6.QtWidgets',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
    ],
    hookspath=[],
    hooksconfig={ },
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    data_paths_dict={},
    overrides={"__file__": None},
    script_name="pythonw.exe",
    script_timestamp=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.data_files,
    a.headers,
    a.hooks,
    a.win32,
    a.imphooksl,
    'Monitor.exe',  # Название exe файла
)

coll = COLLECT(
    exe,
    a.unverted,
    a.binaries,
    a.zipfiles,
    a.aarch64_binaries,
    a.data_files,
    a.dependency_libraries,
    a.scripts,
    excludes=[],
    strip=True,
    upx=True,  # Сжимаем exe с помощью UPX
    name='Monitor',
)
