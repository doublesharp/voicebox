import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { useUIStore } from '../src/stores/uiStore';

const calls: Record<string, any[]> = {
  create: [],
  update: [],
  add: [],
  delete: [],
  effects: [],
  toast: [],
  avatar: [],
};
let fail = '',
  duration = 5,
  profile: any;
const voices = [{ voice_id: 'v1', name: 'Voice One', gender: 'female', language: 'en' }];
const mutation = (key: string, result?: unknown) => ({
  isPending: false,
  mutateAsync: async (arg: unknown) => {
    calls[key].push(arg);
    if (fail === key) throw new Error(`${key} failed`);
    return result;
  },
});
mock.module('../src/lib/hooks/useProfiles', () => ({
  useProfile: () => ({ data: profile }),
  useCreateProfile: () => mutation('create', { id: 'new' }),
  useUpdateProfile: () => mutation('update'),
  useAddSample: () => mutation('add'),
  useDeleteProfile: () => mutation('delete'),
  useUpdateProfileEffects: () => mutation('effects'),
  useUploadAvatar: () => mutation('avatar'),
  useDeleteAvatar: () => mutation('avatar'),
}));
mock.module('../src/lib/queryClient', () => ({ queryClient: {} }));
mock.module('@tanstack/react-query', () => ({ useQuery: () => ({ data: { voices } }) }));
mock.module('../src/lib/api/client', () => ({ apiClient: {} }));
mock.module('../src/lib/hooks/useAudioPlayer', () => ({
  useAudioPlayer: () => ({ isPlaying: false, playPause() {}, cleanup() {} }),
}));
mock.module('../src/lib/hooks/useAudioRecording', () => ({
  useAudioRecording: () => ({ startRecording() {}, stopRecording() {}, cancelRecording() {} }),
}));
mock.module('../src/lib/hooks/useSystemAudioCapture', () => ({
  useSystemAudioCapture: () => ({ startRecording() {}, stopRecording() {}, cancelRecording() {} }),
}));
mock.module('../src/lib/hooks/useTranscription', () => ({
  useTranscription: () => ({ mutateAsync: async () => ({ text: 'transcribed' }) }),
}));
mock.module('../src/lib/utils/audio', () => ({
  getAudioDuration: async () => duration,
  formatAudioDuration: String,
  convertToWav: async () => new Blob(['wav'], { type: 'audio/wav' }),
}));
mock.module('../src/platform/PlatformContext', () => ({
  usePlatform: () => ({ metadata: { isTauri: false } }),
}));
mock.module('../src/components/ui/use-toast', () => ({
  useToast: () => ({ toast: (v: unknown) => calls.toast.push(v) }),
}));
const translate = (key: string) => key;
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
mock.module('../src/components/ui/dialog', () => ({
  Dialog: ({ children, open }: any) => (open ? <div>{children}</div> : null),
  ...Object.fromEntries(
    ['DialogContent', 'DialogDescription', 'DialogHeader', 'DialogTitle'].map((k) => [
      k,
      ({ children }: any) => <div>{children}</div>,
    ]),
  ),
}));
mock.module('../src/components/ui/select', () => ({
  Select: ({ children, ...props }: any) => <select-mock {...props}>{children}</select-mock>,
  ...Object.fromEntries(
    ['SelectContent', 'SelectItem', 'SelectTrigger', 'SelectValue'].map((k) => [
      k,
      ({ children }: any) => <div>{children}</div>,
    ]),
  ),
}));
mock.module('../src/components/ui/tabs', () => ({
  Tabs: ({ children, ...props }: any) => <tabs-mock {...props}>{children}</tabs-mock>,
  ...Object.fromEntries(
    ['TabsContent', 'TabsList', 'TabsTrigger'].map((k) => [
      k,
      ({ children }: any) => <div>{children}</div>,
    ]),
  ),
}));
mock.module('../src/components/VoiceProfiles/AudioSampleUpload', () => ({
  AudioSampleUpload: (props: any) => <audio-upload {...props} />,
}));
mock.module('../src/components/VoiceProfiles/AudioSampleRecording', () => ({
  AudioSampleRecording: () => null,
}));
mock.module('../src/components/VoiceProfiles/AudioSampleSystem', () => ({
  AudioSampleSystem: () => null,
}));
mock.module('../src/components/VoiceProfiles/SampleList', () => ({ SampleList: () => null }));
mock.module('../src/components/Effects/EffectsChainEditor', () => ({
  EffectsChainEditor: (props: any) => <chain-editor {...props} />,
}));
const { ProfileForm } = await import('../src/components/VoiceProfiles/ProfileForm');
const { SampleUpload } = await import('../src/components/VoiceProfiles/SampleUpload');
let view: ReactTestRenderer;
async function mount(editingProfileId: string | null = null) {
  useUIStore.setState({ profileDialogOpen: true, editingProfileId, profileFormDraft: null });
  await act(async () => {
    view = create(<ProfileForm />);
  });
}
const input = (placeholder: string) => view.root.findAllByProps({ placeholder })[0];
async function type(placeholder: string, name: string, value: string) {
  await act(async () => {
    await input(placeholder).props.onChange({ target: { name, value }, type: 'change' });
  });
}
function click(label: string) {
  const node = view.root.findAllByType('button').find((n) => n.children.includes(label));
  if (!node) throw new Error(`missing button ${label}`);
  act(() => node.props.onClick());
}
async function submit() {
  await act(async () => {
    await view.root.findByType('form').props.onSubmit({ preventDefault() {}, persist() {} });
  });
}
const name = (value: string) => type('profileForm.fields.namePlaceholder', 'name', value);
const upload = async (file: File) => {
  await act(async () => {
    view.root.findByType('audio-upload' as never).props.onFileChange(file);
  });
};
afterEach(() => {
  act(() => view?.unmount());
  for (const arr of Object.values(calls)) arr.length = 0;
  fail = '';
  duration = 5;
  profile = undefined;
  useUIStore.setState({ profileDialogOpen: false, editingProfileId: null, profileFormDraft: null });
});
afterAll(() => mock.restore());

test('clone creation validates name, sample and reference before creating', async () => {
  await mount();
  await submit();
  expect(calls.create).toHaveLength(0);
  await name('Clone');
  await submit();
  expect(calls.toast.at(-1).title).toBe('profileForm.toast.sampleRequired');
  await upload(new File(['audio'], 'voice.wav', { type: 'audio/wav' }));
  await submit();
  expect(calls.create).toHaveLength(0);
  await type('profileForm.fields.referenceTextPlaceholder', 'referenceText', 'Hello');
  await submit();
  expect(calls.create[0].name).toBe('Clone');
  expect(calls.add[0].referenceText).toBe('Hello');
  expect(useUIStore.getState().profileDialogOpen).toBe(false);
});

test('long audio is rejected and failed sample upload rolls back', async () => {
  await mount();
  await name('Clone');
  await upload(new File(['audio'], 'voice.wav', { type: 'audio/wav' }));
  await type('profileForm.fields.referenceTextPlaceholder', 'referenceText', 'Hello');
  duration = 31;
  await submit();
  expect(calls.create).toHaveLength(0);
  duration = 5;
  fail = 'add';
  await submit();
  expect(calls.delete).toEqual(['new']);
  expect(useUIStore.getState().profileDialogOpen).toBe(true);
  expect(calls.toast.at(-1).title).toBe('profileForm.toast.sampleFailed');
});

test('designed creation requires a prompt and trims it and personality', async () => {
  await mount();
  click('profileForm.source.design');
  await name('Designed');
  await submit();
  expect(calls.toast.at(-1).title).toBe('profileForm.toast.noDesignPrompt');
  await type('profileForm.design.placeholder', '', '  warm voice  ');
  await type('profileForm.fields.personalityPlaceholder', 'personality', '  kind  ');
  await submit();
  expect(calls.create[0]).toMatchObject({
    voice_type: 'designed',
    design_prompt: 'warm voice',
    default_engine: 'qwen_voice_design',
    personality: 'kind',
  });
});

test('builtin selection is required and submits the selected voice', async () => {
  await mount();
  click('profileForm.source.builtin');
  await name('Preset');
  await submit();
  expect(calls.toast.at(-1).title).toBe('profileForm.toast.noVoiceSelected');
  act(() =>
    view.root
      .findAllByType('button')
      .find((n) => n.findAllByType('div').some((d) => d.children.includes('Voice One')))!
      .props.onClick(),
  );
  await submit();
  expect(calls.create[0]).toMatchObject({
    voice_type: 'preset',
    preset_engine: 'kokoro',
    preset_voice_id: 'v1',
    language: 'en',
  });
});

test('closing and reopening preserves a designed draft', async () => {
  await mount();
  click('profileForm.source.design');
  await name('Draft');
  await type('profileForm.design.placeholder', '', 'warm');
  await act(async () => {
    await view.root
      .findAllByType('button')
      .find((n) => n.children.includes('common.cancel'))!
      .props.onClick();
  });
  expect(useUIStore.getState().profileFormDraft).toMatchObject({
    name: 'Draft',
    voiceSource: 'design',
    designPrompt: 'warm',
  });
  await act(async () => {
    useUIStore.getState().setProfileDialogOpen(true);
  });
  expect(input('profileForm.fields.namePlaceholder').props.value).toBe('Draft');
  expect(input('profileForm.design.placeholder').props.value).toBe('warm');
});

test('editing submits metadata and changed effects; failed save keeps dialog open', async () => {
  profile = { id: 'p', name: 'Old', language: 'en', description: '', effects_chain: [] };
  await mount('p');
  await name('New');
  act(() => view.root.findByType('chain-editor' as never).props.onChange([{ type: 'gain' }]));
  fail = 'effects';
  await submit();
  expect(calls.update[0]).toMatchObject({ profileId: 'p', data: { name: 'New' } });
  expect(calls.effects[0]).toEqual({ profileId: 'p', effectsChain: [{ type: 'gain' }] });
  expect(useUIStore.getState().profileDialogOpen).toBe(true);
  expect(calls.toast.at(-1).title).toBe('profileForm.toast.effectsUpdateFailed');
});

test('sample upload requires a file and text, and sends them to the selected profile', async () => {
  let open = true;
  await act(async () => {
    view = create(
      <SampleUpload
        profileId="p"
        open
        onOpenChange={(v) => {
          open = v;
        }}
      />,
    );
  });
  await submit();
  expect(calls.add).toHaveLength(0);
  const file = new File(['audio'], 'voice.wav', { type: 'audio/wav' });
  await upload(file);
  await submit();
  expect(calls.add).toHaveLength(0);
  await type('Enter the exact text spoken in the audio...', 'referenceText', 'Hello');
  await submit();
  expect(calls.add[0]).toEqual({ profileId: 'p', file, referenceText: 'Hello' });
  expect(open).toBe(false);
});

test('personality-only draft survives closing and reopening', async () => {
  await mount();
  await type('profileForm.fields.personalityPlaceholder', 'personality', 'Gentle');
  await act(async () => {
    await view.root
      .findAllByType('button')
      .find((n) => n.children.includes('common.cancel'))!
      .props.onClick();
  });
  expect(useUIStore.getState().profileFormDraft?.personality).toBe('Gentle');
  await act(async () => {
    useUIStore.getState().setProfileDialogOpen(true);
  });
  expect(input('profileForm.fields.personalityPlaceholder').props.value).toBe('Gentle');
});
