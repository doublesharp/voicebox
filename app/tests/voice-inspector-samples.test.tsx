import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const samples = [
  { id: 's1', reference_text: 'First' },
  { id: 's2', reference_text: 'Second' },
];
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
let request = deferred();
mock.module('../src/lib/hooks/useProfiles', () => ({
  useProfileSamples: () => ({ data: samples, isLoading: false }),
  useUpdateSample: () => ({ mutateAsync: () => request.promise, isPending: false }),
  useDeleteSample: () => ({ mutate() {}, isPending: false }),
  useExportSampleAudio: () => ({ mutate() {}, isPending: false }),
}));
mock.module('../src/lib/api/client', () => ({ apiClient: { getSampleUrl: (id: string) => id } }));
mock.module('../src/components/VoiceProfiles/SampleUpload', () => ({ SampleUpload: () => null }));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast() {} }) }));
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
const { SampleList } = await import('../src/components/VoiceProfiles/SampleList');
let renderer: ReactTestRenderer;
const editButtons = () =>
  renderer.root
    .findAllByType('button')
    .filter((n) => n.props.title === 'sampleList.editTranscription');
const textarea = () => renderer.root.findByType('textarea');
const save = () =>
  renderer.root.findAllByType('button').find((n) => n.children.includes('common.save'))!;
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
