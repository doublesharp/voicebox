import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let projectId = 'one';
let renderer: ReactTestRenderer;
let retryJob: () => Promise<unknown> = async () => {};
let includeJob = false;
const commands: unknown[] = [];
const sessions: any[] = [];
const playbackInstances: Array<{ tick(position: number): void; getPlayhead(): number }> = [];
let scriptRenders = 0;
const project = (id: string) => ({
  id,
  name: `Project ${id}`,
  revision: 0,
  document: { passages: [], clips: [], tracks: [], mix: {} },
  assets: [],
  takes: [],
  jobs: includeJob ? [{ id: 'job', kind: 'render', status: 'failed' }] : [],
  revisions: [],
});
const pass = ({ children }: any) => <>{children}</>;
const storage = new Map<string, string>();
Object.assign(globalThis, {
  localStorage: {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => storage.set(k, v),
    removeItem: (k: string) => storage.delete(k),
  },
  window: { addEventListener() {}, removeEventListener() {}, confirm: () => false },
});
mock.module('@tanstack/react-router', () => ({
  Link: pass,
  useParams: () => ({ projectId }),
  useNavigate: () => () => {},
  useBlocker: () => {},
}));
mock.module('../src/stores/serverStore', () => ({
  useServerStore: (select: any) => select({ serverUrl: 'local' }),
}));
mock.module('../src/stores/playerStore', () => ({
  usePlayerStore: {
    getState: () => ({ setIsPlaying() {}, clearAutoPlayFlag() {} }),
    subscribe: () => () => {},
  },
}));
const platform = { audio: { stopPlayback() {} }, filesystem: { saveFile() {} } };
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => platform }));
mock.module('../src/features/studio/client', () => ({
  StudioApiError: class extends Error {},
  StudioClient: class {
    retryJob() {
      return retryJob();
    }
    profiles() {
      return Promise.resolve([]);
    }
    engines() {
      return Promise.resolve({ items: [] });
    }
  },
}));
mock.module('../src/features/studio/session', () => ({
  operationId: () => 'test-operation',
  errorMessage: (e: any) => e.message,
  ProjectSession: class {
    state: any;
    listeners = new Set<() => void>();
    disposed = false;
    constructor(_client: any, id: string) {
      this.state = { project: project(id), status: 'saved', recoverySaved: true };
      sessions.push(this);
    }
    getSnapshot = () => this.state;
    subscribe = (fn: () => void) => {
      this.listeners.add(fn);
      return () => this.listeners.delete(fn);
    };
    load() {}
    refresh() {}
    dispose() {
      this.disposed = true;
    }
    command(type: string, payload: unknown) {
      commands.push([type, payload]);
      return Promise.resolve();
    }
  },
}));
mock.module('../src/features/studio/review-client', () => ({ ReviewClient: class {} }));
mock.module('../src/features/studio/review-session', () => ({
  ReviewSessions: class {
    subscribe = () => () => {};
    getSnapshot = () => null;
    refresh() {}
    dispose() {}
    unsafeToLeave() {
      return false;
    }
  },
}));
const idle = { mode: 'idle', position: 0 };
mock.module('../src/features/studio/playback', () => ({
  StudioPlayback: class {
    state = idle;
    listeners = new Set<() => void>();
    cursor = 0;
    constructor() {
      playbackInstances.push(this);
    }
    subscribe = (listener: () => void) => {
      this.listeners.add(listener);
      return () => this.listeners.delete(listener);
    };
    getSnapshot = () => this.state;
    getPlayhead = () => this.cursor;
    seek = (position: number) => {
      this.cursor = position;
      this.state = { mode: 'idle', position };
      for (const listener of this.listeners) listener();
    };
    tick(position: number) {
      this.cursor = position;
      this.state = { mode: 'timeline', position };
      for (const listener of this.listeners) listener();
    }
    stop() {}
    dispose() {}
  },
}));
mock.module('../src/components/ui/dialog', () => ({
  Dialog: pass,
  DialogContent: pass,
  DialogDescription: pass,
  DialogHeader: pass,
  DialogTitle: pass,
  DialogTrigger: pass,
}));
mock.module('../src/components/ui/Hint', () => ({ Hint: pass }));
mock.module('../src/components/ui/HelpButton', () => ({ HelpButton: () => null }));
mock.module('../src/features/studio/AudioMeters', () => ({ AudioMeters: () => null }));
mock.module('../src/features/studio/Deliver', () => ({ Deliver: () => null }));
mock.module('../src/features/studio/Director', () => ({ Director: () => null }));
mock.module('../src/features/studio/Review', () => ({ Review: () => null }));
mock.module('../src/features/studio/ScriptEditor', () => ({
  ScriptEditor: () => {
    scriptRenders++;
    return null;
  },
}));
mock.module('../src/features/studio/Timeline', () => ({
  Timeline: () => null,
  Mixer: () => null,
  ClipInspector: () => null,
}));
const { StudioProjectPage } = await import('../src/features/studio/StudioProjectPage');
afterEach(() => {
  act(() => renderer?.unmount());
  commands.length = 0;
  sessions.length = 0;
  projectId = 'one';
  storage.clear();
  includeJob = false;
  retryJob = async () => {};
});
async function mount() {
  await act(async () => {
    renderer = create(<StudioProjectPage />);
  });
}
const nameInput = () => renderer.root.findByProps({ 'aria-label': 'Project name' });
test('blank project name restores the saved name instead of leaving a false empty title', async () => {
  await mount();
  const target = { value: '   ' };
  act(() => nameInput().props.onBlur({ target }));
  expect(target.value).toBe('Project one');
  expect(commands).toEqual([]);
});
test('renaming submits a trimmed name and switching project disposes the old session', async () => {
  await mount();
  act(() => nameInput().props.onBlur({ target: { value: '  New title  ' } }));
  expect(commands).toEqual([['update_project', { name: 'New title' }]]);
  const old = sessions[0];
  projectId = 'two';
  await act(async () => renderer.update(<StudioProjectPage />));
  expect(old.disposed).toBe(true);
  expect(nameInput().props.defaultValue).toBe('Project two');
});

test('failed job mutation is shown and does not refresh project data', async () => {
  includeJob = true;
  retryJob = async () => {
    throw new Error('retry rejected');
  };
  await mount();
  const session = sessions[0];
  let refreshes = 0;
  session.refresh = () => {
    refreshes++;
  };
  const button = renderer.root
    .findAllByType('button')
    .find((node) => node.props.children === 'Retry frozen request')!;
  await act(async () => {
    button.props.onClick();
  });
  expect(JSON.stringify(renderer.toJSON())).toContain('retry rejected');
  expect(refreshes).toBe(0);
});

test('a job completion after switching projects cannot refresh the old project', async () => {
  includeJob = true;
  let resolve!: () => void;
  retryJob = () =>
    new Promise<void>((r) => {
      resolve = r;
    });
  await mount();
  const old = sessions[0];
  let refreshes = 0;
  old.refresh = () => {
    refreshes++;
  };
  const button = renderer.root
    .findAllByType('button')
    .find((node) => node.props.children === 'Retry frozen request')!;
  act(() => button.props.onClick());
  projectId = 'two';
  await act(async () => renderer.update(<StudioProjectPage />));
  await act(async () => resolve());
  expect(old.disposed).toBe(true);
  expect(refreshes).toBe(0);
  expect(nameInput().props.defaultValue).toBe('Project two');
});

test('playback clock updates transport without rerendering script and keeps manual seeks', async () => {
  await mount();
  const player = playbackInstances.at(-1);
  const before = scriptRenders;
  act(() => player.tick(1.25));
  const input = renderer.root
    .findAllByType('input')
    .find((node) => node.props['aria-label'] === 'Playhead seconds')!;
  expect(input.props.value).toBe(1.25);
  expect(scriptRenders).toBe(before);
  act(() => input.props.onChange({ target: { valueAsNumber: 3.5 } }));
  expect(player.getPlayhead()).toBe(3.5);
  expect(input.props.value).toBe(3.5);
  expect(scriptRenders).toBe(before);
});
