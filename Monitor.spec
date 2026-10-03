# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build recipe for Monitor.exe.

Run it through build_exe.bat, or directly:

    python -m PyInstaller Monitor.spec
"""

# datas is empty, and that is the whole point of this comment. Every module
# this app has -- theme, metrics, painter, overlay, history, settings -- is
# reached by an ordinary import from main.py, so PyInstaller's static analysis
# finds all of them and puts them in the archive where the frozen interpreter
# imports them like anything else. datas is for things that are *not* code and
# not imported: icons, .ui files, data tables, a bundled font.
#
# This file used to list overlay.py and metrics.py here, which copied two
# Python sources next to the executable as inert data files. They were then
# both in the archive (from the imports) and on disk beside the exe (from
# here), so the copy on disk was dead weight that a reader could mistake for
# the one actually running. Every module now comes from its import.

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # metrics.py imports these two inside the functions that need them --
    # `import wmi` in the WMI probe and the CPU-clock probe, `import pynvml` in
    # GpuProbe -- so that a machine without a GPU or without WMI still starts.
    #
    # The old comment here said PyInstaller cannot see a function-local import.
    # That is wrong, and it was checked rather than assumed: PyInstaller 6.21's
    # own dependency analysis run over main.py with no hidden imports at all
    # returns both `wmi` and `pynvml` in the graph. It walks function bodies.
    #
    # So why keep them? Because the guarded import is exactly the kind of
    # dependency that disappears without a signal. `try: import wmi / except:
    # pass` succeeds whether or not wmi is present, so moving it into a helper,
    # renaming it, or reaching for importlib instead would drop it from the
    # archive and the build would still succeed -- the absence would surface
    # only at runtime, as dashed rows, on a machine with WMI. Naming them here
    # makes it a stated contract instead of an accident of the analyser's AST
    # walk, and a name that stops resolving is a build-time error
    # ("Hidden import 'wmi' not found") rather than a quiet regression.
    hiddenimports=['wmi', 'pynvml'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # QtWebEngine and the Qt 3D modules are large, are never imported by any
    # module here, and would roughly double the exe for nothing. QtQuick and
    # QtQml go with them: the panel is drawn with QPainter onto a QWidget.
    excludes=[
        'PyQt6.QtWebEngineCore',
        'PyQt6.QtWebEngineWidgets',
        'PyQt6.QtWebEngineQuick',
        'PyQt6.QtQuick',
        'PyQt6.QtQuick3D',
        'PyQt6.QtQml',
        'PyQt6.Qt3DCore',
        'PyQt6.Qt3DRender',
        'PyQt6.Qt3DInput',
        'PyQt6.Qt3DLogic',
        'PyQt6.Qt3DAnimation',
        'PyQt6.Qt3DExtras',
    ],
    noarchive=False,
    # Strip docstrings and asserts. Nothing here asserts, and the docstrings
    # are the best documentation the frozen build has.
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is not installed in this checkout, and PyInstaller skips compression
    # on its own when it is absent rather than failing the build.
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # No console: a windowed build has nowhere for a traceback to go, which is
    # why main.py logs startup failures itself before returning 1.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
