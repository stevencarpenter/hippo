import { expect, test } from "bun:test";
import { fileURLToPath } from "node:url";

test("packages exactly the extension's runtime files", () => {
  const cwd = fileURLToPath(new URL("../", import.meta.url));
  const build = Bun.spawnSync(["npm", "run", "package"], { cwd });
  expect(build.exitCode).toBe(0);

  const checked = Bun.spawnSync(["python3", "-c", `
import json
from pathlib import Path
from zipfile import ZipFile

with ZipFile('web-ext-artifacts/hippo-browser-capture.zip') as archive:
    assert archive.testzip() is None
    files = {name for name in archive.namelist() if not name.endswith('/')}
    manifest = json.loads(archive.read('manifest.json'))
    assert manifest == json.loads(Path('manifest.json').read_text())
    expected = {
        'manifest.json', manifest['browser_action']['default_popup'],
        *manifest['background']['scripts'], *manifest['icons'].values(),
        'dist/content.js', 'dist/popup.js',
    }
    assert files == expected, (files, expected)
    assert all(archive.read(name) for name in files)
`], { cwd });
  expect(checked.stderr.toString()).toBe("");
  expect(checked.exitCode).toBe(0);
}, 30_000);
