import { afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getAvailableEffects: async () => ({
      effects: [
        {
          type: 'gain',
          label: 'Gain',
          params: {
            gain_db: { default: 0, min: -40, max: 40, step: 1, description: 'Gain' },
          },
        },
      ],
    }),
    listEffectPresets: async () => [],
  },
}));
mock.module('../src/components/ui/select', () =>
  Object.fromEntries(
    ['Select', 'SelectContent', 'SelectItem', 'SelectTrigger', 'SelectValue'].map((name) => [
      name,
      (props: { children?: unknown }) => createElement(name, props),
    ]),
  ),
);
mock.module('../src/components/ui/slider', () => ({
  Slider: (props: Record<string, unknown>) => createElement('mock-slider', props),
}));
mock.module('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) => options?.defaultValue ?? key,
  }),
}));
const { EffectsChainEditor } = await import('../src/components/Effects/EffectsChainEditor');
let renderer: ReactTestRenderer;
afterEach(() => act(() => renderer?.unmount()));

function text(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(text).join('');
  if (value && typeof value === 'object' && 'props' in value)
    return text((value.props as { children?: unknown }).children);
  return '';
}

test('Studio effect gestures commit once on release while legacy gestures retain immediate changes', async () => {
  for (const commitParametersOnRelease of [true, false]) {
    const changes: unknown[] = [];
    const query = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    query.setQueryData(['available-effects'], {
      effects: [
        {
          type: 'gain',
          label: 'Gain',
          params: { gain_db: { default: 0, min: -40, max: 40, step: 1, description: 'Gain' } },
        },
      ],
    });
    await act(async () => {
      renderer = create(
        <QueryClientProvider client={query}>
          <EffectsChainEditor
            value={[{ type: 'gain', enabled: true, params: { gain_db: 0 } }]}
            onChange={(chain) => changes.push(chain)}
            showPresets={false}
            commitParametersOnRelease={commitParametersOnRelease}
          />
        </QueryClientProvider>,
      );
      await Promise.resolve();
    });
    const label = renderer.root
      .findAllByType('span')
      .find((node) => text(node.props.children).toLowerCase() === 'gain');
    const expand = label?.parent?.findAllByType('button')[0];
    expect(expand).toBeDefined();
    act(() => expand!.props.onClick());
    const slider = () => renderer.root.findByType('mock-slider' as never);
    act(() => slider().props.onValueChange([5]));
    expect(changes).toHaveLength(commitParametersOnRelease ? 0 : 1);
    if (commitParametersOnRelease) {
      expect(slider().props.value).toEqual([5]);
      act(() => slider().props.onValueCommit([5]));
      expect(changes).toEqual([[{ type: 'gain', enabled: true, params: { gain_db: 5 } }]]);
    }
    act(() => renderer.unmount());
    query.clear();
  }
});

const { Mixer } = await import('../src/features/studio/Timeline');
const { StudioContext } = await import('../src/features/studio/context');
test('busy mixer leaves no apparent unsaved thumb value after acknowledgement', async () => {
  const changes: unknown[] = [];
  const query = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  query.setQueryData(['available-effects'], {
    effects: [
      {
        type: 'gain',
        label: 'Gain',
        params: { gain_db: { default: 0, min: -40, max: 40, step: 1, description: 'Gain' } },
      },
    ],
  });
  const project = {
    document: {
      tracks: [
        {
          id: 'track',
          name: 'Voice',
          gain: 1,
          effects_chain: [{ type: 'gain', enabled: true, params: { gain_db: 0 } }],
        },
      ],
      mix: { normalize: false, target_lufs: -16, max_true_peak_dbtp: -1 },
    },
  };
  const view = (canMutate: boolean) => (
    <QueryClientProvider client={query}>
      <StudioContext.Provider
        value={
          {
            project,
            canMutate,
            session: { command: async (...args: unknown[]) => changes.push(args) },
          } as never
        }
      >
        <Mixer />
      </StudioContext.Provider>
    </QueryClientProvider>
  );
  await act(async () => {
    renderer = create(view(false));
    await Promise.resolve();
  });
  const label = renderer.root
    .findAllByType('span')
    .find((node) => text(node.props.children).toLowerCase() === 'gain');
  act(() => label!.parent!.findAllByType('button')[0].props.onClick());
  const slider = () => renderer.root.findByType('mock-slider' as never);
  expect(renderer.root.findByType('fieldset').props.disabled).toBe(true);
  act(() => slider().props.onValueChange([5]));
  act(() => slider().props.onValueCommit([5]));
  expect(changes).toHaveLength(0);
  await act(async () => renderer.update(view(true)));
  expect(slider().props.value).toEqual([0]);
  query.clear();
});
