import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const remove = mock((_id: string) => {});
const exportVoice = mock((_value: any, _options: any) => {});
const toast = mock((_value: any) => {});
const t = (key: string, options?: any) => options?.defaultValue ?? key;
mock.module('react-i18next', () => ({ initReactI18next: { type: '3rdParty', init() {} }, useTranslation: () => ({ t }) }));
mock.module('../src/lib/hooks/useProfiles', () => ({ useDeleteProfile: () => ({ mutate: remove, isPending: false }), useExportProfile: () => ({ mutate: exportVoice, isPending: false }) }));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
mock.module('../src/components/ui/dialog', () => ({
  Dialog: ({ open, children }: any) => open ? <div>{children}</div> : null,
  ...Object.fromEntries(['DialogContent', 'DialogDescription', 'DialogFooter', 'DialogHeader', 'DialogTitle'].map((key) => [key, ({ children }: any) => <div>{children}</div>])),
}));
const { ProfileCard } = await import('../src/components/VoiceProfiles/ProfileCard');
const { useServerStore } = await import('../src/stores/serverStore');
const { useUIStore } = await import('../src/stores/uiStore');
let view: ReactTestRenderer;
const data = { id: 'p', name: 'Narrator', language: 'en', voice_type: 'cloned', description: '' } as any;
const action = (name: string) => view.root.findByProps({ 'aria-label': name });
const button = (label: string) => view.root.findAllByType('button').find((node) => node.children.includes(label))!;
beforeEach(() => { remove.mockClear(); exportVoice.mockClear(); toast.mockClear(); useServerStore.setState({ serverUrl: 'http://a', serverRevision: 3 }); useUIStore.setState({ selectedProfileId: null, profileDialogOpen: false }); });
afterEach(() => { if (view) act(() => view.unmount()); });
afterAll(() => mock.restore());
async function mount() { await act(async () => { view = create(<ProfileCard profile={data} />); }); }

test('export captures the server and reports errors', async () => {
  await mount();
  act(() => action('profiles.card.export').props.onClick({ stopPropagation() {} }));
  expect(exportVoice).toHaveBeenCalledWith({ profileId: 'p', _serverScope: { serverUrl: 'http://a', serverRevision: 3 } }, expect.any(Object));
  act(() => exportVoice.mock.calls[0][1].onError(new Error('server changed')));
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'Voice export failed', description: 'server changed', variant: 'destructive' }));
});

test('delete requires confirmation and edit opens the selected profile', async () => {
  await mount();
  act(() => action('profiles.card.delete').props.onClick({ stopPropagation() {} }));
  expect(remove).not.toHaveBeenCalled();
  act(() => button('common.delete').props.onClick());
  expect(remove).toHaveBeenCalledWith({ profileId: 'p', _serverScope: { serverUrl: 'http://a', serverRevision: 3 } }, expect.any(Object));
  act(() => action('profiles.card.edit').props.onClick({ stopPropagation() {} }));
  expect(useUIStore.getState().editingProfileId).toBe('p');
  expect(useUIStore.getState().profileDialogOpen).toBe(true);
});

test('deletion confirmation retains the server where the dialog opened', async () => {
  await mount();
  act(() => action('profiles.card.delete').props.onClick({ stopPropagation() {} }));
  act(() => useServerStore.setState({ serverUrl: 'http://b', serverRevision: 4 }));
  act(() => button('common.delete').props.onClick());
  expect(remove.mock.calls[0][0]).toEqual({ profileId: 'p', _serverScope: { serverUrl: 'http://a', serverRevision: 3 } });
});

test('deletion failure is reported to the user', async () => {
  await mount();
  act(() => action('profiles.card.delete').props.onClick({ stopPropagation() {} }));
  act(() => button('common.delete').props.onClick());
  act(() => (remove.mock.calls[0] as any)[1].onError(new Error('server changed')));
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'Voice deletion failed', description: 'server changed', variant: 'destructive' }));
});
