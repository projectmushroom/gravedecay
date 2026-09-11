#!/usr/bin/env python3
"""Bundle the shared web backend and pinned, relocatable CPython for both Macs."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[3]
RELEASE = '20260901'
PYTHON = '3.13.15'
DIGESTS = {
    'aarch64': 'd3904bd6a072246e07aa0bdadee9a14e80521e42a943c0848059feb16a2816dc',
    'x86_64': 'f712a9143c8a5d248438ec7921a0b48d548bca4f1337d33c690d28c2d0504137',
}


def prepare(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('gravedecay.py', 'gravenet.py', 'benchmark.py'):
        shutil.copy2(ROOT / 'dashboard' / name, destination / name)
    for name in ('host.py', 'PYTHON-LICENSES.txt'):
        shutil.copy2(ROOT / 'clients/apple/host' / name, destination / name)
    shutil.copytree(ROOT / 'dashboard/static', destination / 'static', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'web/net', destination / 'net', dirs_exist_ok=True)
    cache = ROOT / 'clients/apple/build/python-cache'
    cache.mkdir(parents=True, exist_ok=True)
    for architecture, digest in DIGESTS.items():
        name = f'cpython-{PYTHON}+{RELEASE}-{architecture}-apple-darwin-install_only_stripped.tar.gz'
        archive = cache / name
        if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            with tempfile.TemporaryDirectory(dir=cache) as temporary:
                download = Path(temporary) / name
                subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--output', str(download),
                                f'https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE}/{name}'], check=True)
                if hashlib.sha256(download.read_bytes()).hexdigest() != digest:
                    raise RuntimeError(f'CPython checksum mismatch: {architecture}')
                download.replace(archive)
        runtime = destination / architecture
        marker = runtime / '.archive-sha256'
        if not marker.exists() or marker.read_text() != digest:
            if runtime.exists():
                shutil.rmtree(runtime)
            runtime.mkdir()
            # These exact checksum-verified upstream archives are trusted build inputs.
            with tarfile.open(archive) as source:
                source.extractall(runtime)
            marker.write_text(digest)
        # The upstream runtime includes its licenses. Sign Mach-O files inside-out
        # with the build identity; never alter a globally installed Python.
        identity = os.environ.get('EXPANDED_CODE_SIGN_IDENTITY') or '-'
        for path in (runtime / 'python').rglob('*'):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open('rb') as stream:
                magic = stream.read(4)
            if magic in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xfe\xed\xfa\xcf'):
                subprocess.run(['codesign', '--force', '--sign', identity, str(path)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    print(f'Bundled native host: {destination}')


if __name__ == '__main__':
    prepare(Path(os.environ['TARGET_BUILD_DIR']) / os.environ['UNLOCALIZED_RESOURCES_FOLDER_PATH'] / 'NativeHost')
