# Desktop runtime

Synfact Transfer retains the upstream application's GPLv2 license.
Bundled dependencies retain their own copyright and license terms.
The build script copies package metadata and license files into runtime/.
The existing THIRD_PARTY_NOTICES.txt covers Python and python-rtmidi.

Qt for Python / PySide6 6.8.3 and Shiboken6 6.8.3:
https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.8.3
License choices reported by the packages: LGPL-3.0-only OR GPL-2.0-only OR
GPL-3.0-only, with commercial terms available separately from The Qt Company.
Qt libraries and QtWebEngine also contain third-party components with their
own notices. Source archives, including QtWebEngine's Chromium notices:
https://download.qt.io/archive/qt/6.8/6.8.3/submodules/
https://code.qt.io/cgit/qt/qtwebengine-chromium.git/

Official licensing information:
https://doc.qt.io/qt-6/licenses-used-in-qt.html
https://doc.qt.io/qtforpython-6/licenses.html

PyInstaller 6.16.0 (build tool): GPL-2.0-or-later with bootloader exception.
https://github.com/pyinstaller/pyinstaller/tree/v6.16.0
python-rtmidi 1.5.8:
https://github.com/SpotlightKid/python-rtmidi/tree/1.5.8

Complete Synfact Transfer application source and build scripts accompany the
binary in the source ZIP. Dependencies are unmodified upstream packages.
