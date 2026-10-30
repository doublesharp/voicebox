import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { useEffectsStore } from '../src/stores/effectsStore';

const list = mock(async (): Promise<any[]> => []);
mock.module('../src/lib/api/client', () => ({ apiClient: { listEffectPresets: list } }));
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
const { EffectsList } = await import('../src/components/EffectsTab/EffectsList');
let view: ReactTestRenderer;
let client: QueryClient;
const flush = async () => { await new Promise((resolve) => setTimeout(resolve, 20)); };
async function mount() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => { view = create(<QueryClientProvider client={client}><EffectsList /></QueryClientProvider>); await flush(); });
}
afterEach(() => { if (view) act(() => view.unmount()); client?.clear(); list.mockReset(); list.mockImplementation(async () => []); useEffectsStore.setState({ selectedPresetId: null, workingChain: [], isCreatingNew: false }); });
afterAll(() => mock.restore());

test('built-in and custom presets are grouped and selecting one copies its effects', async () => {
  const chain = [{ type: 'gain', enabled: true, params: { gain_db: -3 } }];
  list.mockImplementationOnce(async () => [
    { id: 'built', name: 'warm', description: '', is_builtin: true, effects_chain: chain },
    { id: 'custom', name: 'My preset', description: null, is_builtin: false, effects_chain: [] },
  ]);
  await mount(); await act(flush);
  const text = JSON.stringify(view.toJSON());
  expect(text).toContain('effects.sections.builtin');
  expect(text).toContain('effects.sections.custom');
  expect(text).toContain('My preset');
  const preset = view.root.findAllByType('button').find((node) => node.findAllByType('span').some((child) => child.children.includes('My preset')))!;
  act(() => preset.props.onClick());
  expect(useEffectsStore.getState()).toMatchObject({ selectedPresetId: 'custom', workingChain: [], isCreatingNew: false });
  const built = view.root.findAllByType('button').find((node) => node.findAllByType('span').some((child) => child.children.includes('effects.builtinPresets.warm.name')))!;
  act(() => built.props.onClick());
  expect(useEffectsStore.getState()).toMatchObject({ selectedPresetId: 'built', workingChain: chain, isCreatingNew: false });
});

test('creating a preset clears the selected chain', async () => {
  useEffectsStore.setState({ selectedPresetId: 'old', workingChain: [{ type: 'gain', enabled: true, params: {} }] });
  await mount(); await act(flush);
  const createButton = view.root.findAllByType('button').find((node) => node.children.includes('effects.newPreset'))!;
  act(() => createButton.props.onClick());
  expect(useEffectsStore.getState()).toMatchObject({ selectedPresetId: null, workingChain: [], isCreatingNew: true });
  expect(JSON.stringify(view.toJSON())).toContain('effects.unsaved.title');
});

test('loading failure is visible and retry can recover the preset list', async () => {
  list.mockImplementationOnce(async () => { throw new Error('offline'); });
  list.mockImplementationOnce(async () => [{ id: 'recovered', name: 'Recovered', is_builtin: false, effects_chain: [] }]);
  await mount(); await act(flush);
  expect(view.root.findByProps({ role: 'alert' }).children.join('')).toContain('offline');
  const retry = view.root.findAllByType('button').find((node) => node.children.includes('common.retry'))!;
  await act(async () => { retry.props.onClick(); await flush(); });
  expect(JSON.stringify(view.toJSON())).toContain('Recovered');
});
