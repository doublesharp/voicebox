import { afterAll, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create } from 'react-test-renderer';

let profile = {
  id: 'a',
  name: 'Alpha',
  description: '',
  language: 'en',
  effects_chain: [] as unknown[],
};
let finishEffects!: () => void;
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getProfile: async () => ({ ...profile }),
    listProfiles: async () => [{ ...profile }],
    updateProfile: async (_id: string, values: object) => {
      profile = { ...profile, ...values };
      return profile;
    },
    updateProfileEffects: (_id: string, effects: unknown[]) =>
      new Promise((resolve) => {
        finishEffects = () => {
          profile = { ...profile, effects_chain: effects };
          resolve({ ...profile });
        };
      }),
  },
}));
mock.module('../src/components/Effects/EffectsChainEditor', () => ({
  EffectsChainEditor: (props: object) => <chain-editor {...props} />,
}));
mock.module('../src/components/VoiceProfiles/SampleList', () => ({ SampleList: () => null }));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast() {} }) }));
mock.module('../src/components/ui/select', () =>
  Object.fromEntries(
    ['Select', 'SelectContent', 'SelectItem', 'SelectTrigger', 'SelectValue'].map((name) => [
      name,
      (props: { children?: unknown }) => <div>{props.children as never}</div>,
    ]),
  ),
);
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
const { VoiceInspector } = await import('../src/components/VoicesTab/VoiceInspector');
const { profileListKey, profileDetailKey } = await import('../src/lib/hooks/useProfiles');
const { currentServerScope } = await import('../src/lib/serverScope');
afterAll(() => mock.restore());

test('effects acknowledgement refreshes detail and library after the metadata refetch has completed', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(profileDetailKey('a', currentServerScope()), { ...profile });
  client.setQueryData(profileListKey(currentServerScope()), [{ ...profile }]);
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <VoiceInspector profileId="a" />
      </QueryClientProvider>,
    );
  });
  const effects = [{ type: 'gain', enabled: true, params: { gain_db: -3 } }];
  act(() => view.root.findByType('chain-editor' as never).props.onChange(effects));
  let pending!: Promise<void>;
  await act(async () => {
    pending = view.root.findByType('form').props.onSubmit({ preventDefault() {}, persist() {} });
    await Bun.sleep(20);
  });
  expect(client.getQueryData(profileDetailKey('a', currentServerScope()))).toMatchObject({ effects_chain: [] });
  await act(async () => {
    finishEffects();
    await pending;
  });
  expect(client.getQueryData(profileDetailKey('a', currentServerScope()))).toMatchObject({ effects_chain: effects });
  expect(client.getQueryData(profileListKey(currentServerScope()))).toMatchObject([{ effects_chain: effects }]);
  act(() => view.unmount());
  client.clear();
});
