// @vitest-environment node
import { EventEmitter } from 'node:events';
import type { BackendSupervisor } from './backend';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { AGENT_SCAN_TTL_MS, registerRepairAgents, REPAIR_CHANNELS } from './repair-agents';
const mock = vi.hoisted(() => ({
  handlers: new Map<string, (...args: any[]) => any>(),
  spawn: vi.fn(),
  execFile: vi.fn(),
  spawnSync: vi.fn(),
  bridge: vi.fn(),
  close: vi.fn(async () => {}),
  output: vi.fn(),
}));
vi.mock('electron', () => ({
  app: { getPath: () => '/temp', getAppPath: () => '/app' },
  BrowserWindow: {},
  dialog: {},
  ipcMain: {
    handle: (name: string, callback: (...args: any[]) => any) => mock.handlers.set(name, callback),
    removeHandler: (name: string) => mock.handlers.delete(name),
  },
}));
vi.mock('node:fs', () => ({
  existsSync: () => true,
  accessSync: () => {},
  constants: { W_OK: 2 },
  readFileSync: vi.fn(),
  mkdtempSync: vi.fn(),
  rmSync: vi.fn(),
  writeFileSync: vi.fn(),
}));
vi.mock('node:child_process', () => ({
  spawn: mock.spawn,
  execFile: mock.execFile,
  spawnSync: mock.spawnSync,
}));
type ProbeCallback = (error: Error | null, stdout: string, stderr: string) => void;
function answerProbes() {
  mock.execFile.mockImplementation(
    (_file: string, args: string[], _options: unknown, callback: ProbeCallback) => {
      // Only codex is installed; probes answer asynchronously like a real child.
      setTimeout(() =>
        args.includes('--version')
          ? callback(null, 'codex 1.2.3\n', '')
          : args[0] === 'codex'
            ? callback(
                null,
                process.platform === 'win32' ? 'C:\\agents\\codex.exe' : '/usr/bin/codex',
                '',
              )
            : callback(new Error('not found'), '', ''),
      );
      return { stdin: { end: vi.fn() } };
    },
  );
}
vi.mock('./repair-api-bridge', () => ({ startRepairApiBridge: mock.bridge }));
vi.mock('./llm-agent-bridge', () => ({
  startLlmAgentBridge: async () => ({ url: '', token: '', close() {} }),
}));
vi.mock('./window-safety', () => ({ sendToLiveWindow: mock.output }));
const frame = { url: 'app://voicestudio/index.html' };
const contents = { mainFrame: frame };
const owner = { webContents: contents };
const event = { sender: contents, senderFrame: frame };
const request = {
  agent: 'codex',
  mode: 'fix',
  workspace: 'app',
  report: 'Create a preview',
  context: '{}',
};
let dispose: (() => void) | undefined;
beforeEach(() => {
  vi.clearAllMocks();
  answerProbes();
  mock.bridge.mockResolvedValue({ contextFile: '/temp/isolated/context.json', close: mock.close });
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, text: async () => '{}' })),
  );
});
afterEach(() => {
  dispose?.();
  vi.unstubAllGlobals();
});
const start = () => mock.handlers.get(REPAIR_CHANNELS.start)!(event, request);
async function setup() {
  dispose = await registerRepairAgents(
    {
      baseUrl: 'http://localhost',
      requestHeaders: () => ({}),
      status: { stage: 'ready' },
    } as unknown as BackendSupervisor,
    '/source',
    () => owner as any,
  );
}

it('cancels during setup and rejects a concurrent request before launching a process', async () => {
  let release!: (value: unknown) => void;
  mock.bridge.mockReturnValue(
    new Promise((resolve) => {
      release = resolve;
    }),
  );
  await setup();
  const first = start();
  await expect(start()).rejects.toThrow('already running');
  const stopped = mock.handlers.get(REPAIR_CHANNELS.stop)!(event);
  expect(stopped.status).toBe('stopped');
  release({ contextFile: '/temp/isolated/context.json', close: mock.close });
  await first;
  expect(mock.spawn).not.toHaveBeenCalled();
  expect(mock.close).toHaveBeenCalled();
  expect(mock.handlers.get(REPAIR_CHANNELS.state)!(event).status).toBe('stopped');
});
it('launches app chat outside the checkout and closes its capability after streaming completion', async () => {
  const child = Object.assign(new EventEmitter(), {
    stdin: Object.assign(new EventEmitter(), { end: vi.fn() }),
    stdout: new EventEmitter(),
    stderr: new EventEmitter(),
    kill: vi.fn(),
  });
  mock.spawn.mockReturnValue(child);
  await setup();
  const result = await start();
  expect(mock.spawn.mock.calls[0][2].cwd.replaceAll('\\', '/')).toBe('/temp/isolated');
  expect(child.stdin.end).toHaveBeenCalledWith(
    expect.stringContaining('No source checkout is attached'),
  );
  child.stdout.emit('data', Buffer.from('Preview ready'));
  child.emit('close', 0);
  expect(mock.output).toHaveBeenCalledWith(
    owner,
    REPAIR_CHANNELS.event,
    expect.objectContaining({ sessionId: result.sessionId, type: 'output', text: 'Preview ready' }),
  );
  expect(mock.handlers.get(REPAIR_CHANNELS.state)!(event).status).toBe('complete');
  expect(mock.close).toHaveBeenCalled();
});

it('scans CLIs asynchronously and reuses the scan until the TTL or an explicit refresh', async () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  try {
    await setup();
    const list = (options?: { refresh?: boolean }) =>
      mock.handlers.get(REPAIR_CHANNELS.list)!(event, options);
    const pending = list();
    // The handler answers with a promise: the main process never blocks on a probe.
    expect(pending).toBeInstanceOf(Promise);
    const agents = await pending;
    expect(agents.find((agent: { id: string }) => agent.id === 'codex')).toMatchObject({
      available: true,
      version: 'codex 1.2.3',
    });
    const probes = mock.execFile.mock.calls.length;
    await list();
    expect(mock.execFile.mock.calls.length).toBe(probes);
    vi.setSystemTime(Date.now() + 2_000);
    await list({ refresh: true });
    expect(mock.execFile.mock.calls.length).toBeGreaterThan(probes);
    const refreshed = mock.execFile.mock.calls.length;
    vi.setSystemTime(Date.now() + AGENT_SCAN_TTL_MS);
    await list();
    expect(mock.execFile.mock.calls.length).toBeGreaterThan(refreshed);
    expect(mock.spawnSync).not.toHaveBeenCalled();
  } finally {
    vi.useRealTimers();
  }
});
