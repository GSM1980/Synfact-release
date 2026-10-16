"""Build on the target OS: Windows portable directory or macOS .app bundle."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = '0.0.1'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--onefile', action='store_true', help='Optional single executable on Windows')
    parser.add_argument('--quick', action='store_true', help='Build only, without ZIP packaging')
    args = parser.parse_args()
    if sys.platform not in ('win32', 'darwin'):
        parser.error('Build on Windows or macOS, respectively.')
    if args.onefile and sys.platform != 'win32':
        parser.error('macOS is distributed as a complete .app bundle.')

    licenses = ROOT / 'licenses' / 'runtime'
    licenses.mkdir(parents=True, exist_ok=True)
    versions = {}
    runtime_packages = ('PySide6', 'PySide6-Addons', 'PySide6-Essentials', 'shiboken6', 'python-rtmidi', 'pyinstaller')
    for package in (() if args.quick and (licenses / 'BUILD-RUNTIME.json').exists() else runtime_packages):
        distribution = metadata.distribution(package)
        versions[package] = distribution.version
        for item in distribution.files or []:
            if ('.dist-info/' in str(item).replace('\\', '/') and
                    any(word in str(item).lower() for word in ('license', 'copying', 'metadata'))):
                target = licenses / package / Path(str(item)).name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(distribution.locate_file(item), target)
    if versions:
        (licenses / 'BUILD-RUNTIME.json').write_text(json.dumps({
            'python': sys.version, 'platform': platform.platform(), 'architecture': platform.machine(),
            'packages': versions}, indent=2), encoding='utf-8')

    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--windowed',
               '--name', 'SynfactTransfer', '--distpath', str(ROOT / 'dist'),
               '--workpath', str(ROOT / 'work' / 'pyinstaller'), '--specpath', str(ROOT / 'work'),
               '--paths', str(ROOT / 'vendor'), '--hidden-import', 'rtmidi',
               '--exclude-module', 'tkinter', '--noupx',
               '--onefile' if args.onefile else '--onedir']
    for name in ('web', 'assets', 'licenses', 'LICENSE', 'THIRD_PARTY_NOTICES.txt'):
        command.extend(['--add-data', f'{ROOT / name}{":"}{name if (ROOT / name).is_dir() else "."}'])
    if sys.platform == 'darwin':
        command.extend(['--osx-bundle-identifier', 'com.synfact.transfer'])
    command.append(str(ROOT / 'desktop.py'))
    subprocess.run(command, cwd=ROOT, check=True)
    if args.quick:
        return
    release = ROOT / 'releases'
    release.mkdir(exist_ok=True)
    label = 'Windows' if sys.platform == 'win32' else 'macOS'
    stem = f'SynfactTransfer-{VERSION}-{label}-{platform.machine()}' + ('-single' if args.onefile else '')
    stage = ROOT / 'work' / 'release' / stem
    if sys.platform == 'win32':
        artifact = ROOT / 'dist' / ('SynfactTransfer.exe' if args.onefile else 'SynfactTransfer')
    else:
        artifact = ROOT / 'dist' / 'SynfactTransfer.app'
    # ditto preserves macOS bundle permissions and framework symlinks.
    archive = release / (stem + '.zip')
    if sys.platform == 'darwin':
        stage.mkdir(parents=True, exist_ok=True)
        shutil.copytree(artifact, stage / artifact.name, dirs_exist_ok=True, symlinks=True)
        for name in ('readme.md', 'LICENSE', 'THIRD_PARTY_NOTICES.txt'):
            shutil.copy2(ROOT / name, stage / name)
        shutil.copytree(ROOT / 'licenses', stage / 'licenses', dirs_exist_ok=True)
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(stage), str(archive)], check=True)
    else:
        # Write directly: staging a second Qt tree can exceed Windows MAX_PATH.
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            files = artifact.rglob('*') if artifact.is_dir() else [artifact]
            for item in files:
                if item.is_file():
                    bundle.write(item, item.relative_to(artifact.parent))
            for name in ('readme.md', 'LICENSE', 'THIRD_PARTY_NOTICES.txt'):
                bundle.write(ROOT / name, name)
            for item in (ROOT / 'licenses').rglob('*'):
                if item.is_file():
                    bundle.write(item, item.relative_to(ROOT))
    print(archive)


if __name__ == '__main__':
    main()
