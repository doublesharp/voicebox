import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { useEffectsStore } from '../src/stores/effectsStore';
import { usePlayerStore } from '../src/stores/playerStore';

const chain = [{ type: 'gain', enabled: true, params: { gain_db: 1 } }];
const presets = [
  { id: 'a', name: 'Alpha', description: 'A', is_builtin: false, effects_chain: chain },
  { id: 'b', name: 'Beta', description: 'B', is_builtin: false, effects_chain: chain },
];
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
let createRequest = deferred<(typeof presets)[number]>();
let deleteRequest = deferred<void>();
let previewRequest = deferred<Blob>();
const played: string[] = [];
const creates: unknown[] = [];
const updates: unknown[] = [];
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    listEffectPresets: async () => presets,
    createEffectPreset: (values: unknown) => {
      creates.push(values);
      return createRequest.promise;
    },
    updateEffectPreset: async (id: string, values: unknown) => {
      updates.push([id, values]);
    },
    deleteEffectPreset: () => deleteRequest.promise,
    previewEffects: () => previewRequest.promise,
  },
}));
mock.module('../src/lib/hooks/useHistory', () => ({
  useHistory: () => ({ data: { items: [{ id: 'g', status: 'completed' }] } }),
}));
mock.module('../src/components/Effects/EffectsChainEditor', () => ({
  EffectsChainEditor: (props: object) => createElement('chain-editor', props),
}));
mock.module('../src/components/Effects/GenerationPicker', () => ({
  GenerationPicker: (props: object) => createElement('generation-picker', props),
}));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast: () => {} }) }));
mock.module('../src/components/ui/dialog', () =>
  Object.fromEntries(
    [
      'Dialog',
      'DialogContent',
      'DialogDescription',
      'DialogFooter',
      'DialogHeader',
      'DialogTitle',
    ].map((name) => [name, (props: { children?: unknown }) => createElement(name, props)]),
  ),
);
mock.module('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: { defaultValue?: string }) => opts?.defaultValue ?? key,
  }),
}));
const { EffectsDetail } = await import('../src/components/EffectsTab/EffectsDetail');
let renderer: ReactTestRenderer;
let client: QueryClient;
async function mount(id: string | null, creating = false) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  for (const preset of presets) client.setQueryData(['effect-preset', preset.id], preset);
  useEffectsStore.setState({ selectedPresetId: id, isCreatingNew: creating, workingChain: chain });
  usePlayerStore.setState({
    setAudioWithAutoPlay: (url: string) => {
      played.push(url);
    },
  } as never);
  await act(async () => {
    renderer = create(
      <QueryClientProvider client={client}>
        <EffectsDetail />
      </QueryClientProvider>,
    );
  });
}
const input = () => renderer.root.findAllByType('input')[0];
const button = (label: string) =>
  renderer.root.findAllByType('button').find((node) => node.children.includes(label))!;
function select(id: string) {
  useEffectsStore.getState().setSelectedPresetId(id);
  useEffectsStore.getState().setWorkingChain(chain);
}
afterEach(() => {
  act(() => renderer?.unmount());
  client?.clear();
  played.length = 0;
  creates.length = 0;
  updates.length = 0;
});
afterAll(() => mock.restore());

test('refetch preserves an unsaved name while switching presets loads the selected name', async () => {
  await mount('a');
  act(() => input().props.onChange({ target: { value: 'My draft' } }));
  await act(async () => {
    client.setQueryData(['effect-preset', 'a'], { ...presets[0], description: 'refetched' });
    await Bun.sleep(20);
  });
  expect(input().props.value).toBe('My draft');
  await act(async () => select('b'));
  expect(input().props.value).toBe('Beta');
});

test('a pending new preset save does not change a later selection', async () => {
  createRequest = deferred();
  await mount(null, true);
  act(() => input().props.onChange({ target: { value: 'New' } }));
  act(() => {
    void button('effects.detail.savePreset').props.onClick();
  });
  await act(async () => select('b'));
  await act(async () => {
    createRequest.resolve({ ...presets[0], id: 'created' });
    await createRequest.promise;
  });
  expect(useEffectsStore.getState().selectedPresetId).toBe('b');
  expect(input().props.value).toBe('Beta');
});

test('a pending delete does not clear a later selection', async () => {
  deleteRequest = deferred();
  await mount('a');
  act(() => {
    void button('common.delete').props.onClick();
  });
  await act(async () => select('b'));
  await act(async () => {
    deleteRequest.resolve();
    await deleteRequest.promise;
  });
  expect(useEffectsStore.getState().selectedPresetId).toBe('b');
  expect(useEffectsStore.getState().workingChain).toEqual(chain);
});

test('a preview completing after selection changes does not start playback', async () => {
  previewRequest = deferred();
  await mount('a');
  act(() => {
    void button('effects.preview.button').props.onClick();
  });
  await act(async () => select('b'));
  await act(async () => {
    previewRequest.resolve(new Blob(['audio']));
    await previewRequest.promise;
  });
  expect(played).toEqual([]);
});

test('seeded selection, edit, and refetch sequences preserve the active draft', async () => {
  for (const seed of [17, 20261029, 0xc0ffee]) {
    let random = seed >>> 0;
    const next = () => {
      random = (Math.imul(random, 1664525) + 1013904223) >>> 0;
      return random;
    };
    await mount('a');
    let selected = 'a';
    let expected = 'Alpha';
    for (let step = 0; step < 40; step++) {
      const action = next() % 3;
      if (action === 0) {
        selected = selected === 'a' ? 'b' : 'a';
        expected = selected === 'a' ? 'Alpha' : 'Beta';
        await act(async () => select(selected));
      } else if (action === 1) {
        expected = `draft-${seed}-${step}`;
        act(() => input().props.onChange({ target: { value: expected } }));
      } else {
        const original = presets.find((p) => p.id === selected)!;
        await act(async () => {
          client.setQueryData(['effect-preset', selected], {
            ...original,
            description: `refresh-${step}`,
          });
          await Bun.sleep(2);
        });
      }
      expect({
        selected: useEffectsStore.getState().selectedPresetId,
        name: input().props.value,
      }).toEqual({ selected, name: expected });
    }
    act(() => renderer.unmount());
    client.clear();
  }
});

test('switching away and back invalidates a pending delete', async () => {
  deleteRequest = deferred();
  await mount('a');
  act(() => {
    void button('common.delete').props.onClick();
  });
  await act(async () => select('b'));
  await act(async () => select('a'));
  await act(async () => {
    deleteRequest.resolve();
    await deleteRequest.promise;
  });
  expect(useEffectsStore.getState().selectedPresetId).toBe('a');
});

test('edits made during a create remain available after it completes', async () => {
  createRequest = deferred();
  await mount(null, true);
  act(() => input().props.onChange({ target: { value: 'First' } }));
  act(() => {
    void button('effects.detail.savePreset').props.onClick();
  });
  act(() => input().props.onChange({ target: { value: 'Later draft' } }));
  await act(async () => {
    createRequest.resolve({ ...presets[0], id: 'created' });
    await createRequest.promise;
  });
  expect(useEffectsStore.getState().isCreatingNew).toBe(false);
  expect(useEffectsStore.getState().selectedPresetId).toBe('created');
  expect(input().props.value).toBe('Later draft');
  await act(async () => {
    await button('common.save').props.onClick();
    await Bun.sleep(5);
  });
  expect(creates).toHaveLength(1);
  expect(updates).toEqual([['created', expect.objectContaining({ name: 'Later draft' })]]);
});

test('a preview completing after unmount does not start playback', async () => {
  previewRequest = deferred();
  await mount('a');
  act(() => {
    void button('effects.preview.button').props.onClick();
  });
  act(() => renderer.unmount());
  await act(async () => {
    previewRequest.resolve(new Blob(['audio']));
    await previewRequest.promise;
  });
  expect(played).toEqual([]);
});

test('closing save-as while creation is pending does not change the selection', async () => {
  createRequest = deferred();
  await mount('a');
  await act(async () => {
    client.setQueryData(['effect-preset', 'a'], { ...presets[0], is_builtin: true });
    await Bun.sleep(10);
  });
  act(() => button('effects.detail.saveAsCustom').props.onClick());
  const dialog = renderer.root.findByType('Dialog' as never);
  const dialogName = renderer.root.findAllByType('input')[0];
  act(() => dialogName.props.onChange({ target: { value: 'Copy' } }));
  act(() => {
    void button('common.save').props.onClick();
  });
  act(() => dialog.props.onOpenChange(false));
  await act(async () => {
    createRequest.resolve({ ...presets[0], id: 'created' });
    await createRequest.promise;
  });
  expect(useEffectsStore.getState().selectedPresetId).toBe('a');
});
