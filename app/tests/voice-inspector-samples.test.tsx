import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const samples = [
  { id: 's1', reference_text: 'First' },
  { id: 's2', reference_text: 'Second' },
];
function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((r, j) => {
    resolve = r;
    reject = j;
  });
  return { promise, resolve, reject };
}
let request = deferred();
const toast = mock((_value: any) => {});
const deleteMutation = mock((_arg: any, _options?: any) => {});
const exportMutation = mock((_arg: any, _options?: any) => {});
const updateMutation = mock((_arg: any) => request.promise);
mock.module('../src/lib/hooks/useProfiles', () => ({
  useProfileSamples: () => ({ data: samples, isLoading: false }),
  useUpdateSample: () => ({ mutateAsync: updateMutation, isPending: false }),
  useDeleteSample: () => ({ mutate: deleteMutation, isPending: false }),
  useExportSampleAudio: () => ({ mutate: exportMutation, isPending: false }),
}));
mock.module('../src/lib/api/client', () => ({ apiClient: { getSampleUrl: (id: string) => id } }));
mock.module('../src/components/VoiceProfiles/SampleUpload', () => ({ SampleUpload: () => null }));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
mock.module('../src/components/ui/slider', () => ({ Slider: () => null }));
for (const [path, names] of [
  [
    '../src/components/ui/dialog',
    ['Dialog', 'DialogContent', 'DialogDescription', 'DialogFooter', 'DialogHeader', 'DialogTitle'],
  ],
  [
    '../src/components/ui/dropdown-menu',
    ['DropdownMenu', 'DropdownMenuContent', 'DropdownMenuItem', 'DropdownMenuTrigger'],
  ],
] as const)
  mock.module(path, () =>
    Object.fromEntries(
      names.map((name) => [name, (props: { children?: unknown }) => createElement(name, props)]),
    ),
  );
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
const oldAudio = globalThis.Audio;
globalThis.Audio = class {
  duration = 0;
  currentTime = 0;
  src = '';
  addEventListener() {}
  removeEventListener() {}
  pause() {}
  play() {}
} as never;
const { useServerStore } = await import('../src/stores/serverStore');
const { SampleList } = await import('../src/components/VoiceProfiles/SampleList');
let renderer: ReactTestRenderer;
const editButtons = () =>
  renderer.root
    .findAllByType('button')
    .filter((n) => n.props.title === 'sampleList.editTranscription');
const textarea = () => renderer.root.findByType('textarea');
const save = () =>
  renderer.root.findAllByType('button').find((n) => n.children.includes('common.save'))!;
beforeEach(() => { toast.mockClear(); deleteMutation.mockClear(); exportMutation.mockClear(); updateMutation.mockClear(); useServerStore.setState({ serverUrl: 'http://server-a', serverRevision: 0 }); });
afterEach(() => act(() => renderer?.unmount()));
afterAll(() => {
  globalThis.Audio = oldAudio;
  mock.restore();
});

test('a pending sample save cannot close a later sample edit', async () => {
  request = deferred();
  await act(async () => {
    renderer = create(<SampleList profileId="a" />);
  });
  act(() => editButtons()[0].props.onClick());
  act(() => {
    void save().props.onClick();
  });
  act(() => editButtons()[0].props.onClick());
  act(() => textarea().props.onChange({ target: { value: 'Second draft' } }));
  await act(async () => {
    request.resolve();
    await request.promise;
  });
  expect(textarea().props.value).toBe('Second draft');
});

test('switching profiles closes delete confirmation and clears sample edits', async () => {
  await act(async () => {
    renderer = create(<SampleList profileId="a" />);
  });
  const deleteButton = renderer.root
    .findAllByType('button')
    .find((n) => n.props.title === 'sampleList.deleteSample')!;
  act(() => deleteButton.props.onClick());
  expect(renderer.root.findByType('Dialog' as never).props.open).toBe(true);
  act(() => editButtons()[0].props.onClick());
  await act(async () => {
    renderer.update(<SampleList profileId="b" />);
  });
  expect(renderer.root.findAllByType('textarea')).toHaveLength(0);
  expect(renderer.root.findByType('Dialog' as never).props.open).toBe(false);
});

test('sample controls become visible when their group contains keyboard focus', async () => {
  await act(async () => {
    renderer = create(<SampleList profileId="a" />);
  });
  const button = editButtons()[0];
  expect(button.props.type).toBe('button');
  expect(
    renderer.root
      .findAllByType('div')
      .some((node) => node.props.className?.includes('group-focus-within:opacity-100')),
  ).toBe(true);
});

test('a failed previous sample edit does not report an error over a newer edit', async () => {
  request = deferred();
  await act(async () => { renderer = create(<SampleList profileId="a" />); });
  act(() => editButtons()[0].props.onClick());
  act(() => { void save().props.onClick(); });
  act(() => editButtons()[0].props.onClick());
  act(() => textarea().props.onChange({ target: { value: 'New draft' } }));
  await act(async () => { request.reject(new Error('old save failed')); await request.promise.catch(() => {}); });
  expect(textarea().props.value).toBe('New draft');
  expect(toast).not.toHaveBeenCalled();
});

test('changing servers clears a pending delete confirmation and edit draft', async () => {
  await act(async () => { renderer = create(<SampleList profileId="a" />); });
  const deleteButton = renderer.root.findAllByType('button').find((n) => n.props.title === 'sampleList.deleteSample')!;
  act(() => deleteButton.props.onClick());
  act(() => editButtons()[0].props.onClick());
  act(() => useServerStore.setState({ serverUrl: 'http://server-b', serverRevision: 1 }));
  expect(renderer.root.findAllByType('textarea')).toHaveLength(0);
  expect(renderer.root.findByType('Dialog' as never).props.open).toBe(false);
});

test('sample edits carry the server scope captured when editing began', async () => {
  request = deferred();
  await act(async () => { renderer = create(<SampleList profileId="a" />); });
  act(() => editButtons()[0].props.onClick());
  act(() => { void save().props.onClick(); });
  expect(updateMutation).toHaveBeenCalledWith({ sampleId: 's1', referenceText: 'First', _serverScope: { serverUrl: 'http://server-a', serverRevision: 0 } });
  await act(async () => { request.resolve(); await request.promise; });
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'sampleList.toast.updated' }));
});

test('sample deletion captures the server and reports a current failure', async () => {
  await act(async () => { renderer = create(<SampleList profileId="a" />); });
  const deleteButton = renderer.root.findAllByType('button').find((n) => n.props.title === 'sampleList.deleteSample')!;
  act(() => deleteButton.props.onClick());
  const confirm = renderer.root.findAllByType('button').find((n) => n.children.includes('common.delete'))!;
  act(() => confirm.props.onClick());
  expect(deleteMutation).toHaveBeenCalledWith({ sampleId: 's1', _serverScope: { serverUrl: 'http://server-a', serverRevision: 0 } }, expect.any(Object));
  act(() => deleteMutation.mock.calls[0][1].onError(new Error('delete failed')));
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ description: 'delete failed', variant: 'destructive' }));
});

test('sample export captures the current server scope', async () => {
  await act(async () => { renderer = create(<SampleList profileId="a" />); });
  const choices = renderer.root.findAllByType('DropdownMenuItem' as never);
  act(() => choices[0].props.onSelect());
  expect(exportMutation).toHaveBeenCalledWith(expect.objectContaining({ sampleId: 's1', format: 'mp3', _serverScope: { serverUrl: 'http://server-a', serverRevision: 0 } }), expect.any(Object));
});
