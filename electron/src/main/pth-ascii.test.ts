import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { asciiSafePthFiles, asciiSafePthText, pythonLiteral } from './pth-ascii';

const CJK_USER = '\u5f20\u4e09'; // a typical non-English Windows username

function python(): string | null {
  for (const candidate of ['python3', 'python']) {
    const probe = spawnSync(candidate, ['-c', 'import sys; print(sys.version_info[0])'], {
      encoding: 'utf8',
    });
    if (probe.status === 0 && probe.stdout.trim() === '3') return candidate;
  }
  return null;
}

describe('asciiSafePthFiles (#1783)', () => {
  it('rewrites a non-ASCII editable path line and leaves ASCII files untouched', async () => {
    const venv = mkdtempSync(join(tmpdir(), 'vs-pth-'));
    const posix = join(venv, 'lib', 'python3.11', 'site-packages');
    const windows = join(venv, 'Lib', 'site-packages');
    mkdirSync(posix, { recursive: true });
    mkdirSync(windows, { recursive: true });
    const project = join(venv, CJK_USER, 'runtime', 'project');
    writeFileSync(join(posix, '_editable_impl_omnivoice.pth'), project, 'utf8');
    writeFileSync(join(windows, '_editable_impl_omnivoice.pth'), project, 'utf8');
    const ascii = 'import _virtualenv';
    writeFileSync(join(posix, '_virtualenv.pth'), ascii, 'utf8');

    const rewritten = await asciiSafePthFiles(venv);

    // Case-insensitive file systems see one directory under both spellings.
    expect(rewritten.length).toBeGreaterThanOrEqual(1);
    for (const file of rewritten) {
      const bytes = readFileSync(file);
      expect(bytes.every((byte) => byte < 0x80)).toBe(true);
      expect(bytes.toString('utf8')).toContain('\\u5f20\\u4e09');
    }
    expect(readFileSync(join(posix, '_virtualenv.pth'), 'utf8')).toBe(ascii);
    expect(await asciiSafePthFiles(venv)).toEqual([]);
  });

  it('drops non-ASCII comments and escapes code lines', () => {
    expect(asciiSafePthText(`# ${CJK_USER}\n./lib`)).toBe('./lib');
    expect(asciiSafePthText(`import os; x = '${CJK_USER}'`)).toBe(
      "import builtins; builtins.exec('import os; x = \\'\\u5f20\\u4e09\\'')",
    );
    expect(pythonLiteral('C:\\Users\\\u00e9\ud83d\ude00')).toBe(
      "'C:\\\\Users\\\\\\xe9\\U0001f600'",
    );
  });

  it.skipIf(!python())('lets site add the original directory under an ASCII locale', () => {
    const venv = mkdtempSync(join(tmpdir(), 'vs-pth-site-'));
    const sitePackages = join(venv, 'site-packages');
    const target = join(venv, CJK_USER, 'project');
    mkdirSync(sitePackages, { recursive: true });
    mkdirSync(target, { recursive: true });
    const pth = join(sitePackages, '_editable_impl_omnivoice.pth');
    const script =
      'import site, sys; before = len(sys.path); ' +
      `site.addpackage(sys.argv[1], '_editable_impl_omnivoice.pth', set()); ` +
      'print(sys.version_info[1], sys.path[before:] == [sys.argv[2]])';
    // Python 3.11 decodes .pth files in the locale encoding even in UTF-8
    // mode; an ASCII locale reproduces the cp936 failure off Windows while
    // UTF-8 mode keeps the file-system encoding able to name the directory.
    const env = { ...process.env, LC_ALL: 'C', LANG: 'C', PYTHONCOERCECLOCALE: '0', PYTHONUTF8: '1' };
    const run = () =>
      spawnSync(python()!, ['-c', script, sitePackages, target], { encoding: 'utf8', env });

    writeFileSync(pth, target, 'utf8');
    const before = run();
    const minor = Number(before.stdout.split(' ')[0]);
    // 3.13+ reads .pth files as UTF-8 first, so only older interpreters fail.
    if (before.status !== 0 || minor < 13) expect(before.stdout.trim().endsWith('True')).toBe(false);

    writeFileSync(pth, asciiSafePthText(target), 'utf8');
    const after = run();
    expect(after.stderr).toBe('');
    expect(after.stdout.trim().endsWith('True')).toBe(true);
  });
});
