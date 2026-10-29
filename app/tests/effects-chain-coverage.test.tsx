import { afterAll, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';
import { act, create } from 'react-test-renderer';
import type { EffectConfig } from '../src/lib/api/types';

mock.module('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: { defaultValue?: string }) => opts?.defaultValue ?? key,
  }),
}));
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getAvailableEffects: async () => ({ effects: [] }),
    listEffectPresets: async () => [],
  },
}));
mock.module('../src/components/ui/select', () => ({
  Select: (props: Record<string, never>) => <select-control {...props} />,
  SelectTrigger: (props: Record<string, never>) => <div {...props} />,
  SelectContent: (props: Record<string, never>) => <div {...props} />,
  SelectItem: (props: Record<string, never>) => <option {...props} />,
  SelectValue: (props: Record<string, never>) => <span {...props} />,
}));
mock.module('../src/components/ui/slider', () => ({
  Slider: (props: Record<string, never>) => <slider-control {...props} />,
}));
const { EffectsChainEditor } = await import('../src/components/Effects/EffectsChainEditor');
afterAll(() => mock.restore());

test('add, edit, toggle, remove and clear update the rendered chain', async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(['available-effects'], {
    effects: [
      {
        type: 'gain',
        label: 'Gain',
        params: { db: { default: 1, min: 0, max: 10, step: 1, description: 'Gain' } },
      },
    ],
  });
  client.setQueryData(['effect-presets'], []);
  function Harness() {
    const [chain, setChain] = useState<EffectConfig[]>([]);
    return <EffectsChainEditor value={chain} onChange={setChain} />;
  }
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <Harness />
      </QueryClientProvider>,
    );
  });
  const selects = () => view.root.findAllByType('select-control' as never);
  act(() => selects()[1].props.onValueChange('unknown'));
  expect(view.root.findAllByProps({ title: 'effects.chain.remove' })).toHaveLength(0);
  act(() => selects()[1].props.onValueChange('gain'));
  expect(view.root.findAllByProps({ title: 'effects.chain.remove' })).toHaveLength(1);
  expect(view.root.findByType('slider-control' as never).props.value).toEqual([1]);
  act(() => view.root.findByType('slider-control' as never).props.onValueChange([6]));
  expect(view.root.findByType('slider-control' as never).props.value).toEqual([6]);
  act(() => view.root.findByProps({ title: 'effects.chain.disable' }).props.onClick());
  expect(view.root.findByProps({ title: 'effects.chain.enable' })).toBeDefined();
  act(() => view.root.findByProps({ title: 'effects.chain.remove' }).props.onClick());
  expect(view.root.findAllByProps({ title: 'effects.chain.remove' })).toHaveLength(0);
  act(() => selects()[1].props.onValueChange('gain'));
  const clear = view.root
    .findAllByType('button')
    .find((n) => n.children.includes('effects.chain.clear'))!;
  act(() => clear.props.onClick());
  expect(view.root.findAllByProps({ title: 'effects.chain.remove' })).toHaveLength(0);
  act(() => view.unmount());
  client.clear();
});

test('reorder retains effect identity and release commits only the final parameter', async () => {
  const { SortableContext } = await import('@dnd-kit/sortable');
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(['available-effects'], {
    effects: [
      {
        type: 'gain',
        label: 'Gain',
        params: { db: { default: 1, min: 0, max: 10, step: 1, description: 'Gain' } },
      },
      { type: 'echo', label: 'Echo', params: {} },
    ],
  });
  client.setQueryData(['effect-presets'], []);
  const changes: EffectConfig[][] = [];
  function Harness() {
    const [chain, setChain] = useState<EffectConfig[]>([
      { type: 'gain', enabled: true, params: { db: 2 } },
      { type: 'echo', enabled: true, params: {} },
    ]);
    return (
      <EffectsChainEditor
        value={chain}
        commitParametersOnRelease
        onChange={(next) => {
          changes.push(next);
          setChain(next);
        }}
      />
    );
  }
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <Harness />
      </QueryClientProvider>,
    );
  });
  act(() => view.root.findByProps({ 'aria-label': 'Expand Gain' }).props.onClick());
  const slider = () => view.root.findByType('slider-control' as never);
  act(() => slider().props.onValueChange([7]));
  expect(slider().props.value).toEqual([7]);
  expect(changes).toHaveLength(0);
  act(() => slider().props.onValueCommit([7]));
  expect(changes.at(-1)?.[0].params.db).toBe(7);
  const ids = view.root.findByType(SortableContext).props.items;
  const drag = view.root.findAll((node) => typeof node.props.onDragEnd === 'function')[0].props
    .onDragEnd;
  act(() => drag({ active: { id: ids[0] }, over: null }));
  expect(changes).toHaveLength(1);
  act(() => drag({ active: { id: ids[0] }, over: { id: ids[1] } }));
  expect(changes.at(-1)?.map((effect) => effect.type)).toEqual(['echo', 'gain']);
  expect(view.root.findByProps({ 'aria-label': 'Collapse Gain' })).toBeDefined();
  expect(slider().props.value).toEqual([7]);
  act(() => view.unmount());
  client.clear();
});

test('preset selection replaces the chain and excluded effects cannot be added', async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(['available-effects'], {
    effects: [{ type: 'gain', label: 'Gain', params: {} }],
  });
  client.setQueryData(
    ['effect-presets'],
    [
      {
        id: 'p',
        name: 'Warm',
        description: 'Preset',
        is_builtin: false,
        effects_chain: [{ type: 'gain', enabled: true, params: {} }],
      },
    ],
  );
  const changes = mock(() => {});
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <EffectsChainEditor value={[]} onChange={changes} excludedTypes={['gain']} />
      </QueryClientProvider>,
    );
  });
  const selects = view.root.findAllByType('select-control' as never);
  act(() => selects[1].props.onValueChange('gain'));
  expect(changes).not.toHaveBeenCalled();
  act(() => selects[0].props.onValueChange('missing'));
  expect(changes).not.toHaveBeenCalled();
  act(() => selects[0].props.onValueChange('p'));
  expect(changes).toHaveBeenCalledWith([{ type: 'gain', enabled: true, params: {} }]);
  act(() => view.unmount());
  client.clear();
});
