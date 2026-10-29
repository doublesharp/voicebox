import { afterAll, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create } from 'react-test-renderer';
import type { CaptureResponse } from '../src/lib/api/types';

const row = (
  id: string,
  raw: string,
  refined: string | null,
  source: CaptureResponse['source'],
): CaptureResponse =>
  ({
    id,
    transcript_raw: raw,
    transcript_refined: refined,
    source,
    created_at: '2026-01-01T00:00:00Z',
    duration_ms: 61000,
    language: 'en',
    stt_model: 'whisper',
    llm_model: null,
  }) as CaptureResponse;
let rows = [
  row('a', 'Alpha raw', 'Alpha polished', 'dictation'),
  row('b', 'Beta raw', null, 'file'),
];
const upload = mock(() => {});
const refine = mock(() => {});
const toggle = mock(() => {});
const toast = mock(() => {});
const deleteCapture = mock(async (id: string) => {
  rows = rows.filter((r) => r.id !== id);
});
const generateSpeech = mock(async () => ({ id: 'generation-1' }));
const writeText = mock(async () => {});
const save = mock(async () => '/captures/result.txt' as string | null);
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    listCaptures: async () => ({ items: rows, total: rows.length }),
    listProfiles: async () => [],
    deleteCapture,
    generateSpeech,
    getCaptureAudioUrl: (id: string) => `/audio/${id}`,
  },
}));
mock.module('../src/lib/hooks/useSettings', () => ({
  useCaptureSettings: () => ({ settings: {}, update() {} }),
}));
mock.module('../src/lib/hooks/useDictationReadiness', () => ({
  useDictationReadiness: () => ({ canRecord: true }),
}));
mock.module('../src/lib/hooks/useCaptureRecordingSession', () => ({
  useCaptureRecordingSession: () => ({
    pillState: 'hidden',
    isRecording: false,
    isUploading: false,
    uploadFile: upload,
    refine,
    toggleRecording: toggle,
  }),
}));
mock.module('../src/components/CapturesTab/CaptureInlinePlayer', () => ({
  CaptureInlinePlayer: ({ audioUrl }: { audioUrl: string }) => <div data-audio={audioUrl} />,
}));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
mock.module('../src/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuLabel: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuSeparator: () => <hr />,
  DropdownMenuItem: ({ children, onClick }: { children: React.ReactNode; onClick: () => void }) => (
    <button type="button" onClick={onClick}>
      {children}
    </button>
  ),
}));
mock.module('@tanstack/react-router', () => ({
  Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
    <a href={to}>{children}</a>
  ),
}));
mock.module('@tauri-apps/api/event', () => ({ listen: async () => () => {} }));
mock.module('@tauri-apps/plugin-dialog', () => ({ save }));
mock.module('@tauri-apps/plugin-fs', () => ({
  writeFile: async () => {},
  writeTextFile: writeText,
}));
mock.module('react-i18next', () => ({
  initReactI18next: { type: '3rdParty', init() {} },
  useTranslation: () => ({ t: (key: string) => key }),
}));
const { CapturesTab } = await import('../src/components/CapturesTab/CapturesTab');
const originalNavigator = globalThis.navigator;
afterAll(() => {
  globalThis.navigator = originalNavigator;
  mock.restore();
});
const button = (view: ReturnType<typeof create>, label: string) =>
  view.root.findAllByType('button').find((n) => n.children.includes(label))!;
async function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  client.setQueryData(['captures'], { items: rows, total: rows.length });
  client.setQueryData(['profiles'], []);
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <CapturesTab />
      </QueryClientProvider>,
    );
  });
  return { client, view };
}
test('search and selection remain independent across refetch and transcript modes', async () => {
  rows = [row('a', 'Alpha raw', 'Alpha polished', 'dictation'), row('b', 'Beta raw', null, 'file')];
  const { client, view } = await setup();
  expect(view.root.findByType('textarea').props.defaultValue).toBe('Alpha polished');
  act(() => button(view, 'captures.transcript.raw').props.onClick());
  expect(view.root.findByType('textarea').props.defaultValue).toBe('Alpha raw');
  const search = view.root
    .findAllByType('input')
    .find((n) => n.props.placeholder === 'captures.searchPlaceholder')!;
  act(() => search.props.onChange({ target: { value: '  BETA ' } }));
  expect(view.root.findAllByProps({ 'data-audio': '/audio/a' })).toHaveLength(1);
  const beta = view.root
    .findAllByType('button')
    .find((n) => n.findAllByType('div').some((d) => d.children.includes('Beta raw')))!;
  act(() => beta.props.onClick());
  expect(view.root.findByType('textarea').props.defaultValue).toBe('Beta raw');
  expect(button(view, 'captures.transcript.refined').props.disabled).toBe(true);
  await act(async () => {
    client.setQueryData(['captures'], { items: [...rows], total: 2 });
  });
  expect(view.root.findByProps({ 'data-audio': '/audio/b' })).toBeDefined();
  act(() => search.props.onChange({ target: { value: 'missing' } }));
  expect(
    view.root.findAllByType('p').some((n) => n.children.includes('captures.empty.noMatches')),
  ).toBe(true);
  act(() => view.unmount());
  client.clear();
});
test('copy failure, import, refine and transcript export use the selected capture', async () => {
  const { client, view } = await setup();
  globalThis.navigator = {
    clipboard: {
      writeText: async () => {
        throw new Error('denied');
      },
    },
  } as Navigator;
  await act(async () => {
    await button(view, 'captures.actions.copy').props.onClick();
  });
  expect(toast).toHaveBeenCalledWith({
    title: 'captures.toast.copyFailed',
    variant: 'destructive',
  });
  act(() => button(view, 'captures.actions.reRefine').props.onClick());
  expect(refine).toHaveBeenCalledWith('a');
  const file = new File(['audio'], 'recording.wav', { type: 'audio/wav' });
  const input = view.root.findAllByType('input').find((n) => n.props.type === 'file')!;
  expect(input.props.accept).toContain('.wav');
  const target = { files: [file], value: 'recording.wav' };
  act(() => input.props.onChange({ target }));
  expect(upload).toHaveBeenCalledWith(file, 'file');
  expect(target.value).toBe('');
  await act(async () => {
    await button(view, 'captures.actions.exportTranscript').props.onClick();
  });
  expect(writeText).toHaveBeenCalledWith('/captures/result.txt', 'Alpha polished');
  act(() => button(view, 'captures.actions.dictate').props.onClick());
  expect(toggle).toHaveBeenCalled();
  expect(view.root.findByProps({ href: '/settings/captures' })).toBeDefined();
  act(() => view.unmount());
  client.clear();
});
