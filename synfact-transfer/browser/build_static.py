"""Prepare browser-only assets for a static host."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
out = root / 'browser' / 'dist'
(out / 'browser').mkdir(parents=True, exist_ok=True)
(out / 'web').mkdir(exist_ok=True)
page = (root / 'web' / 'index.html').read_text(encoding='utf-8')
page = page.replace('<head>', '<head><meta name="session-token" content="browser">', 1)
page = page.replace('href="style.css"', 'href="/web/style.css"')
page = page.replace('<script src="app.js" defer></script>', '<script type="module" src="/browser/native-client.mjs"></script>')
(out / 'index.html').write_text(page, encoding='utf-8')
for name in ('app.js', 'style.css'):
    shutil.copyfile(root / 'web' / name, out / 'web' / name)
for name in ('native-client.mjs', 'native-engine.mjs', 'midi.mjs', 'digitone.mjs', 'samples.mjs', 'collection.mjs', 'drop.mjs'):
    shutil.copyfile(root / 'browser' / name, out / 'browser' / name)
print(f'Static browser files: {out}')
