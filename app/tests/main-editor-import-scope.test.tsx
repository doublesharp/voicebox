import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { createElement, type ReactNode } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { useServerStore } from '../src/stores/serverStore';

const toast = mock((_value: unknown) => {});
const mutate = mock((_value: unknown, _callbacks: Record<string, () => void>) => {});
const pass = ({ children }: { children: ReactNode }) => <>{children}</>;
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
mock.module('../src/components/Branding', () => ({ DoubleSharpMark: () => null }));
mock.module('../src/components/Generation/FloatingGenerateBox', () => ({
  FloatingGenerateBox: () => null,
}));
mock.module('../src/components/History/HistoryTable', () => ({ HistoryTable: () => null }));
mock.module('../src/components/VoiceProfiles/ProfileForm', () => ({ ProfileForm: () => null }));
mock.module('../src/components/ui/button', () => ({
  Button: (props: Record<string, unknown>) => createElement('button', { type: 'button', ...props }),
}));
mock.module('../src/components/ui/dialog', () => ({
  Dialog: ({ open, children }: { open: boolean; children: ReactNode }) =>
    open ? <div data-dialog>{children}</div> : null,
  DialogContent: pass,
  DialogDescription: pass,
  DialogFooter: pass,
  DialogHeader: pass,
  DialogTitle: pass,
}));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
mock.module('../src/lib/hooks/useProfiles', () => ({
  useProfiles: () => ({ data: [{ id: 'voice' }], isLoading: false }),
  useImportProfile: () => ({ isPending: false, mutate }),
}));
mock.module('../src/stores/uiStore', () => ({
  useUIStore: (select: (state: unknown) => unknown) => select({ setProfileDialogOpen() {} }),
}));
const { MainEditor } = await import('../src/components/MainEditor/MainEditor');
let view: ReactTestRenderer;
function mount() {
  useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 });
  act(() => {
    view = create(<MainEditor />, {
      createNodeMock: (node) => (node.type === 'input' ? { value: '', click() {} } : null),
    });
  });
}
function selectFile(name: string) {
  const file = new File(['archive'], name);
  act(() => view.root.findByType('input').props.onChange({ target: { files: [file] } }));
  return file;
}
function confirm() {
  const button = view.root
    .findAllByType('button')
    .find((node) => node.props.children === 'main.import.action');
  expect(button).toBeDefined();
  return button!.props.onClick as () => void;
}
afterEach(() => {
  act(() => view?.unmount());
  useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 });
  mutate.mockClear();
  toast.mockClear();
});
afterAll(() => mock.restore());

test('a file selected on one server cannot be confirmed after switching servers', () => {
  mount();
  selectFile('voice.voicebox.zip');
  const oldConfirm = confirm();
  act(() => {
    useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
  });
  act(() => oldConfirm());
  expect(mutate).not.toHaveBeenCalled();
  expect(view.root.findAllByProps({ 'data-dialog': true })).toHaveLength(0);
});

test('an old import completion cannot close a newer server import or show a stale toast', () => {
  mount();
  selectFile('old.voicebox.zip');
  act(() => confirm()());
  const oldCallbacks = mutate.mock.calls[0][1] as {
    onSuccess: () => void;
    onError: (error: Error) => void;
  };
  act(() => {
    useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 });
  });
  selectFile('new.voicebox.zip');
  act(() => {
    oldCallbacks.onSuccess();
    oldCallbacks.onError(new Error('old failure'));
  });
  expect(view.root.findAllByProps({ 'data-dialog': true })).toHaveLength(1);
  expect(toast).not.toHaveBeenCalled();
});

test('invalid profile archives show an error without opening a confirmation', () => {
  mount();
  selectFile('voice.zip');
  expect(mutate).not.toHaveBeenCalled();
  expect(view.root.findAllByProps({ 'data-dialog': true })).toHaveLength(0);
  expect(toast).toHaveBeenCalledTimes(1);
});

test('valid import binds the selected file and server and ignores a double confirmation', () => {
  mount();
  const file = selectFile('voice.voicebox.zip');
  const click = confirm();
  act(() => {
    click();
    click();
  });
  expect(mutate).toHaveBeenCalledTimes(1);
  expect(mutate.mock.calls[0][0]).toEqual({
    file,
    _serverScope: { serverUrl: 'http://server-a', serverRevision: 0 },
  });
  act(() => {
    (mutate.mock.calls[0][1] as { onSuccess: () => void }).onSuccess();
  });
  expect(view.root.findAllByProps({ 'data-dialog': true })).toHaveLength(0);
  expect(toast).toHaveBeenCalledTimes(1);
});

test('cancelling and choosing a new archive ignores an earlier completion', () => {
  mount();
  selectFile('old.voicebox.zip');
  act(() => confirm()());
  const old = mutate.mock.calls[0][1] as { onSuccess: () => void };
  const cancel = view.root
    .findAllByType('button')
    .find((node) => node.props.children === 'common.cancel');
  act(() => cancel!.props.onClick());
  selectFile('new.voicebox.zip');
  act(() => old.onSuccess());
  expect(view.root.findAllByProps({ 'data-dialog': true })).toHaveLength(1);
  expect(toast).not.toHaveBeenCalled();
  act(() => confirm()());
  expect(mutate).toHaveBeenCalledTimes(2);
});

test('same-server import errors preserve the dialog and allow a retry', () => {
  mount();
  selectFile('voice.voicebox.zip');
  act(() => confirm()());
  const callbacks = mutate.mock.calls[0][1] as { onError: (error: Error) => void };
  act(() => callbacks.onError(new Error('invalid archive')));
  expect(view.root.findAllByProps({ 'data-dialog': true })).toHaveLength(1);
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ description: 'invalid archive' }));
  act(() => confirm()());
  expect(mutate).toHaveBeenCalledTimes(2);
});

test('a late earlier completion cannot unlock a newer in-flight import', () => {
  mount();
  selectFile('old.voicebox.zip');
  act(() => confirm()());
  const old = mutate.mock.calls[0][1] as { onSuccess: () => void };
  const cancel = view.root
    .findAllByType('button')
    .find((node) => node.props.children === 'common.cancel');
  act(() => cancel!.props.onClick());
  selectFile('new.voicebox.zip');
  const newConfirm = confirm();
  act(() => newConfirm());
  expect(mutate).toHaveBeenCalledTimes(2);
  act(() => old.onSuccess());
  act(() => newConfirm());
  expect(mutate).toHaveBeenCalledTimes(2);
  expect(view.root.findAllByProps({ 'data-dialog': true })).toHaveLength(1);
  expect(toast).not.toHaveBeenCalled();
});

test('a reconnection to the same URL invalidates the old selection', () => {
  mount();
  selectFile('voice.voicebox.zip');
  const oldConfirm = confirm();
  act(() => {
    useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 1 });
  });
  act(() => oldConfirm());
  expect(mutate).not.toHaveBeenCalled();
  expect(view.root.findAllByProps({ 'data-dialog': true })).toHaveLength(0);
});
