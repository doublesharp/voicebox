import { afterEach, expect, test } from 'bun:test';
import type { ReactNode } from 'react';
import { useSyncExternalStore } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { StudioClient } from '../src/features/studio/client';
import { StudioContext, type StudioContextValue } from '../src/features/studio/context';
import type { StudioPlayback } from '../src/features/studio/playback';
import { ProjectSession } from '../src/features/studio/session';
import { TakeWorkflow } from '../src/features/studio/TakeWorkflow';
import type { StudioCommand, StudioProject } from '../src/features/studio/types';

let renderer: ReactTestRenderer;
let session: ProjectSession;
afterEach(() => {
  act(() => renderer?.unmount());
  session?.dispose();
});
function text(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(text).join('');
  return '';
}
function button(label: string) {
  const found = renderer.root
    .findAllByType('button')
    .find((node) => text(node.props.children) === label);
  expect(found).toBeDefined();
  return found!;
}
function project(): StudioProject {
  return {
    id: 'p',
    name: 'Comparison',
    revision: 0,
    client_label: null,
    created_at: '',
    updated_at: '',
    document: {
      schema_version: 1,
      sample_rate: 48000,
      channels: 2,
      language: 'en',
      passages: [
        {
          id: 'line',
          text: 'Current words',
          language: 'en',
          profile_id: null,
          voice_snapshot: null,
          direction: { instruct: '', seed: null, max_chunk_chars: 300, crossfade_ms: 0 },
          input_revision: 0,
          selection_token: 0,
          selected_take_id: 'a',
          locked: false,
        },
      ],
      tracks: [{ id: 'track', name: 'Voice', kind: 'voice', mute: false, solo: false, gain: 1 }],
      clips: [
        {
          id: 'clip',
          passage_id: 'line',
          take_id: 'a',
          asset_id: 'asset-a',
          track_id: 'track',
          start_sample: 777,
          trim_start_sample: 30000,
          trim_end_sample: 0,
          gain: 0.7,
          fade_in_samples: 0,
          fade_out_samples: 0,
        },
      ],
      mix: { normalize: false, target_lufs: -16, max_true_peak_dbtp: -1 },
    },
    takes: ['a', 'b'].map((id) => ({
      id,
      passage_id: 'line',
      asset_id: `asset-${id}`,
      text: `Recorded ${id}`,
      engine: `engine-${id}`,
      model_size: null,
      created_at: '',
      request: { passage: { direction: { instruct: `Direction ${id}` } } },
    })),
    assets: ['a', 'b'].map((id) => ({
      id: `asset-${id}`,
      hash: id,
      frames: id === 'a' ? 96000 : 24000,
      sample_rate: 48000,
      channels: 1,
      duration: id === 'a' ? 2 : 0.5,
      size_bytes: 1,
    })),
    jobs: [],
    revisions: [],
    approvals: [],
    deliverables: [],
  };
}
async function mount(options: { failNotes?: boolean; failLevels?: boolean } = {}) {
  let current = project();
  const commands: StudioCommand[] = [];
  const reads: string[] = [];
  const auditions: Array<{ url: string; gain: number | undefined }> = [];
  let stops = 0;
  const client = new StudioClient('http://test', (async (url, init) => {
    if (String(url).endsWith('/commands')) {
      const command = JSON.parse(String(init?.body)) as StudioCommand;
      commands.push(command);
      if (options.failNotes) return Response.json({ detail: 'Disk unavailable' }, { status: 503 });
      current = structuredClone(current);
      current.revision++;
      if (command.type === 'annotate_take')
        current.document.take_annotations = {
          b: { name: String(command.payload.name), note: String(command.payload.note) },
        };
      return Response.json(current);
    }
    if (String(url).endsWith('/audition-level')) {
      reads.push(String(url));
      if (options.failLevels) return Response.json({ detail: 'Not measurable' }, { status: 422 });
      const rms = String(url).includes('asset-a') ? 0.4 : 0.1;
      return Response.json({
        method: 'sample_rms_v1',
        status: 'measured',
        rms,
        peak: rms,
        frames: 96000,
        measured_frames: 96000,
        sample_rate: 48000,
        channels: 1,
      });
    }
    return Response.json(current);
  }) as typeof fetch);
  const storage = new Map<string, string>();
  session = new ProjectSession(client, 'p', {
    get length() {
      return storage.size;
    },
    key: (index) => [...storage.keys()][index] ?? null,
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => {
      storage.set(key, value);
    },
    removeItem: (key) => {
      storage.delete(key);
    },
  });
  await session.load();
  const playback = {
    stop: () => {
      stops++;
    },
    audition: async (url: string, _label: string, gain?: number) => {
      auditions.push({ url, gain });
    },
  } as unknown as StudioPlayback;
  function Harness({ showTakes = true }: { showTakes?: boolean }) {
    const save = useSyncExternalStore(session.subscribe, session.getSnapshot);
    const context = {
      client,
      project: save.project!,
      save,
      session,
      playback,
      canMutate: ['saved', 'dirty'].includes(save.status),
      actionBusy: false,
    } as StudioContextValue;
    return (
      <StudioContext.Provider value={context}>
        {showTakes && (
          <TakeWorkflow passage={save.project!.document.passages[0]} currentText="Current words" />
        )}
      </StudioContext.Provider>
    );
  }
  await act(async () => {
    renderer = create(<Harness />);
  });
  return {
    commands,
    reads,
    auditions,
    stops: () => stops,
    showTakes: (showTakes: boolean) => renderer.update(<Harness showTakes={showTakes} />),
    remoteNotes: (note: string) => {
      current = structuredClone(current);
      current.revision++;
      current.document.take_annotations = { b: { name: 'Remote pick', note } };
    },
  };
}
async function pair() {
  for (const [side, value] of [
    ['A', 'a'],
    ['B', 'b'],
  ])
    await act(async () => {
      renderer.root
        .findByProps({ 'aria-label': `Comparison take ${side}` })
        .props.onChange({ target: { value } });
    });
}
function matchControl() {
  return renderer.root
    .findAllByType('input')
    .find(
      (node) =>
        node.props.type === 'checkbox' &&
        text(node.parent?.props.children).includes('Match listening'),
    )!;
}
function replaceControl() {
  return renderer.root
    .findAllByType('input')
    .find(
      (node) =>
        node.props.type === 'checkbox' &&
        text(node.parent?.props.children).includes('Replace linked'),
    )!;
}
test('two explicitly chosen takes show recorded snapshots and match only temporary audition gains', async () => {
  const result = await mount();
  expect(result.reads).toHaveLength(0);
  expect(matchControl().props.disabled).toBe(true);
  await pair();
  const content = JSON.stringify(renderer.toJSON());
  expect(content).toContain('Recorded a');
  expect(content).toContain('Recorded b');
  expect(content).toContain('Direction a');
  expect(content).toContain('Direction b');
  await act(async () => {
    matchControl().props.onChange({ target: { checked: true } });
  });
  expect(result.reads).toHaveLength(2);
  await act(async () => {
    button('Play A').props.onClick();
    button('Play B').props.onClick();
  });
  expect(result.auditions).toEqual([
    { url: 'http://test/studio/v1/assets/asset-a/audio', gain: 0.25 },
    { url: 'http://test/studio/v1/assets/asset-b/audio', gain: 1 },
  ]);
  expect(result.commands).toHaveLength(0);
  await act(async () => {
    matchControl().props.onChange({ target: { checked: false } });
    button('Stop comparison').props.onClick();
  });
  expect(result.stops()).toBeGreaterThan(1);
  await act(async () => {
    button('Play A').props.onClick();
  });
  expect(result.auditions.at(-1)?.gain).toBe(1);
});
test('unmeasurable matching blocks matched playback and leaves ordinary audition available', async () => {
  const result = await mount({ failLevels: true });
  await pair();
  await act(async () => {
    matchControl().props.onChange({ target: { checked: true } });
  });
  expect(button('Play A').props.disabled).toBe(true);
  expect(JSON.stringify(renderer.toJSON())).toContain('Not measurable');
  expect(result.auditions).toHaveLength(0);
  await act(async () => {
    matchControl().props.onChange({ target: { checked: false } });
  });
  expect(button('Play A').props.disabled).toBe(false);
});
test('naming a retained take sends an undoable command through the recovery journal', async () => {
  const result = await mount();
  act(() =>
    renderer.root
      .findByProps({ 'aria-label': 'Take 2 name' })
      .props.onChange({ target: { value: 'Client pick' } }),
  );
  act(() =>
    renderer.root
      .findByProps({ 'aria-label': 'Take 2 notes' })
      .props.onChange({ target: { value: 'Unhurried ending' } }),
  );
  await act(async () => {
    await button('Save take notes').props.onClick();
  });
  expect(result.commands[0]).toMatchObject({
    expected_revision: 0,
    type: 'annotate_take',
    payload: { take_id: 'b', name: 'Client pick', note: 'Unhurried ending' },
  });
  expect(JSON.stringify(renderer.toJSON())).toContain('Client pick');
});
test('failed notes remain visible alongside an actionable save error', async () => {
  await mount({ failNotes: true });
  act(() =>
    renderer.root
      .findByProps({ 'aria-label': 'Take 2 notes' })
      .props.onChange({ target: { value: 'Do not lose this' } }),
  );
  await act(async () => {
    await button('Save take notes').props.onClick();
  });
  expect(renderer.root.findByProps({ 'aria-label': 'Take 2 notes' }).props.value).toBe(
    'Do not lose this',
  );
  expect(JSON.stringify(renderer.toJSON())).toContain('Take notes were not saved');
  expect(session.recoveryCopy()).toContain('Do not lose this');
});
test('annotation keystrokes enter the durable journal before Save is clicked', async () => {
  await mount();
  act(() =>
    renderer.root
      .findByProps({ 'aria-label': 'Take 2 notes' })
      .props.onChange({ target: { value: 'Pending before Save' } }),
  );
  expect(session.getSnapshot().status).toBe('dirty');
  expect(session.recoveryCopy()).toContain('Pending before Save');
});
test('annotation drafts survive switching away from the take inspector', async () => {
  const result = await mount();
  act(() =>
    renderer.root
      .findByProps({ 'aria-label': 'Take 2 notes' })
      .props.onChange({ target: { value: 'Keep while switching passages' } }),
  );
  act(() => result.showTakes(false));
  act(() => result.showTakes(true));
  expect(renderer.root.findByProps({ 'aria-label': 'Take 2 notes' }).props.value).toBe(
    'Keep while switching passages',
  );
});
test('a remote annotation edit cannot silently rebase an unsaved local annotation', async () => {
  const result = await mount();
  act(() =>
    renderer.root
      .findByProps({ 'aria-label': 'Take 2 notes' })
      .props.onChange({ target: { value: 'Local pending note' } }),
  );
  result.remoteNotes('Newer note from another window');
  await act(async () => {
    await session.refresh();
  });
  expect(session.getSnapshot().status).toBe('conflict');
  expect(session.getSnapshot().project?.revision).toBe(0);
  await act(async () => {
    await button('Save take notes').props.onClick();
  });
  expect(result.commands).toHaveLength(0);
  expect(session.recoveryCopy()).toContain('Local pending note');
});
test.each([
  'Reset trims',
  'Keep timeline audio',
])('short replacement requires the explicit %s choice before any command', async (choice) => {
  const result = await mount();
  act(() => replaceControl().props.onChange({ target: { checked: true } }));
  await act(async () => {
    await button('Use take').props.onClick();
  });
  expect(result.commands).toHaveLength(0);
  expect(renderer.root.findAllByProps({ 'aria-label': 'Shorter take replacement' })).toHaveLength(
    1,
  );
  await act(async () => {
    await button(choice).props.onClick();
  });
  expect(result.commands[0].payload).toEqual({
    passage_id: 'line',
    take_id: 'b',
    replace_clips: choice === 'Reset trims',
    ...(choice === 'Reset trims' ? { reset_trims: true } : {}),
  });
});
