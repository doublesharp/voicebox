import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { createElement, forwardRef, type ReactNode } from 'react';
import { Controller } from 'react-hook-form';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { create as createStore } from 'zustand';

const ui = createStore<{
  selectedProfileId: string | null;
  selectedEngine: string;
  setSelectedProfileId: (id: string | null) => void;
  setSelectedEngine: (engine: string) => void;
}>((set) => ({
  selectedProfileId: 'a',
  selectedEngine: 'qwen',
  setSelectedProfileId: (id) => set({ selectedProfileId: id }),
  setSelectedEngine: (engine) => set({ selectedEngine: engine }),
}));
let profiles = [
  {
    id: 'a',
    name: 'A',
    language: 'en',
    default_engine: 'chatterbox',
    voice_type: 'cloned',
    personality: 'A',
    effects_chain: [{ type: 'a' }],
  },
  {
    id: 'b',
    name: 'B',
    language: 'en',
    default_engine: 'qwen',
    voice_type: 'cloned',
    personality: 'B',
    effects_chain: [{ type: 'b' }],
  },
];
const presets = [{ id: 'custom', name: 'Custom', effects_chain: [{ type: 'custom' }] }];
const initialProfiles = structuredClone(profiles);
let compose: (id: string) => Promise<{ text: string }> = async () => ({ text: 'composed' });
let status: Promise<{ models: never[] }> | undefined;
let statusCalls = 0;
const requests: Record<string, unknown>[] = [];
const toasts: Record<string, unknown>[] = [];
let generationError: Error | undefined;
let storyRoute = false;
let hiddenProfile = false;
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    composeWithPersonality: (id: string) => compose(id),
    listEffectPresets: async () => presets,
    getModelStatus: async () => {
      statusCalls++;
      return status ?? { models: [] };
    },
  },
}));
mock.module('@tanstack/react-query', () => ({
  useQuery: () => ({ data: presets }),
  useMutation: ({
    mutationFn,
    onError,
  }: {
    mutationFn: (id: string) => Promise<unknown>;
    onError: (error: Error) => void;
  }) => ({
    isPending: false,
    mutateAsync: async (id: string) => {
      try {
        return await mutationFn(id);
      } catch (e) {
        onError(e as Error);
        throw e;
      }
    },
  }),
}));
mock.module('@tanstack/react-router', () => ({
  useMatchRoute: () => () => storyRoute,
  Link: passLink,
}));
function passLink({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
mock.module('framer-motion', () => ({
  motion: {
    div: forwardRef<HTMLDivElement, { children: ReactNode }>(({ children }, ref) => (
      <div ref={ref}>{children}</div>
    )),
  },
  AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
mock.module('../src/lib/hooks/useProfiles', () => ({
  useProfile: (id: string) => ({
    data: hiddenProfile ? undefined : profiles.find((p) => p.id === id),
  }),
  useProfiles: () => ({ data: profiles }),
}));
mock.module('../src/lib/hooks/useStories', () => ({ useStory: () => ({ data: undefined }) }));
mock.module('../src/lib/hooks/useGeneration', () => ({
  useGeneration: () => ({
    isPending: false,
    mutateAsync: async (request: Record<string, unknown>) => {
      requests.push(request);
      if (generationError) throw generationError;
      return { id: 'generated' };
    },
  }),
}));
mock.module('../src/lib/hooks/useSettings', () => ({
  useGenerationSettings: () => ({ settings: {} }),
}));
mock.module('../src/lib/hooks/useModelDownloadToast', () => ({ useModelDownloadToast() {} }));
mock.module('../src/components/ui/use-toast', () => ({
  useToast: () => ({ toast: (value: Record<string, unknown>) => toasts.push(value) }),
}));
mock.module('../src/stores/uiStore', () => ({ useUIStore: ui }));
mock.module('../src/stores/storyStore', () => ({
  useStoryStore: (s: (x: unknown) => unknown) => s({ selectedStoryId: null, trackEditorHeight: 0 }),
}));
mock.module('../src/stores/generationStore', () => ({
  useGenerationStore: (s: (x: unknown) => unknown) =>
    s({ addPendingStoryAdd() {}, addPendingGeneration() {} }),
}));
const pass = ({ children }: { children: ReactNode }) => <>{children}</>;
mock.module('../src/components/ui/form', () => ({
  Form: pass,
  FormControl: pass,
  FormItem: pass,
  FormLabel: pass,
  FormMessage: () => null,
  FormField: Controller,
}));
mock.module('../src/components/ui/button', () => ({
  Button: (props: Record<string, unknown>) => createElement('button', { type: 'button', ...props }),
}));
mock.module('../src/components/ui/textarea', () => ({
  Textarea: forwardRef((props: Record<string, unknown>, ref) =>
    createElement('textarea', { ...props, ref }),
  ),
}));
mock.module('../src/components/ui/select', () => ({
  Select: (props: Record<string, unknown>) => createElement('mock-select', props),
  SelectContent: pass,
  SelectItem: pass,
  SelectTrigger: pass,
  SelectValue: () => null,
}));
mock.module('../src/components/Generation/EngineModelSelector', () => ({
  getEngineDescription: () => 'Engine capabilities',
  EngineModelSelector: ({ form }: { form: { setValue: (key: string, value: string) => void } }) => (
    <button
      type="button"
      aria-label="choose-engine"
      onClick={() => form.setValue('engine', 'luxtts')}
    />
  ),
}));
mock.module('../src/components/Generation/SpeechSettings', () => ({ SpeechSettings: () => null }));
mock.module('../src/components/Generation/ParalinguisticInput', () => ({
  ParalinguisticInput: () => null,
}));
// The renderer has no DOM; click-away listeners are irrelevant to these state tests.
(globalThis as unknown as { document: unknown }).document = {
  addEventListener() {},
  removeEventListener() {},
};
const { FloatingGenerateBox } = await import('../src/components/Generation/FloatingGenerateBox');
const { useGenerationDraftStore } = await import('../src/stores/generationDraftStore');
let renderer: ReactTestRenderer;
function mount() {
  act(() => {
    renderer = create(<FloatingGenerateBox />);
  });
}
function rerender() {
  act(() => renderer.update(<FloatingGenerateBox />));
}
function chooseEffects(id: string) {
  const select = renderer.root
    .findAllByType('mock-select' as never)
    .find((x) => ['none', '_profile', 'custom'].includes(x.props.value));
  act(() => select!.props.onValueChange(id));
}
function type(text: string) {
  act(() => {
    renderer.root.findByType('textarea').props.onChange({ target: { value: text, name: 'text' } });
  });
}
afterEach(() => {
  act(() => renderer?.unmount());
  useGenerationDraftStore.setState({
    values: null,
    textRevision: 0,
    submitting: false,
    profileId: null,
    profileApplied: false,
    presetId: null,
    effectsOverridden: false,
  });
  ui.setState({ selectedProfileId: 'a', selectedEngine: 'qwen' });
  requests.length = 0;
  toasts.length = 0;
  generationError = undefined;
  status = undefined;
  statusCalls = 0;
  storyRoute = false;
  hiddenProfile = false;
  profiles = structuredClone(initialProfiles);
});

test('late profile data does not overwrite engine and effects chosen while loading', () => {
  hiddenProfile = true;
  mount();
  act(() => renderer.root.findByProps({ 'aria-label': 'choose-engine' }).props.onClick());
  chooseEffects('custom');
  hiddenProfile = false;
  rerender();
  expect(useGenerationDraftStore.getState().values?.engine).toBe('luxtts');
  expect(useGenerationDraftStore.getState().presetId).toBe('custom');
});
afterAll(() => mock.restore());

test('refetch and route remount preserve deliberate engine and effects choices', () => {
  mount();
  act(() => renderer.root.findByProps({ 'aria-label': 'choose-engine' }).props.onClick());
  chooseEffects('custom');
  type('unfinished');
  profiles = profiles.map((p) => ({ ...p }));
  rerender();
  expect(useGenerationDraftStore.getState().values).toMatchObject({
    text: 'unfinished',
    engine: 'luxtts',
  });
  expect(useGenerationDraftStore.getState().presetId).toBe('custom');
  act(() => renderer.unmount());
  storyRoute = true;
  mount();
  expect(renderer.root.findByType('textarea').props.value).toBe('unfinished');
  expect(useGenerationDraftStore.getState().values?.engine).toBe('luxtts');
  expect(useGenerationDraftStore.getState().presetId).toBe('custom');
  act(() => ui.getState().setSelectedProfileId('b'));
  expect(useGenerationDraftStore.getState().values?.engine).toBe('qwen');
  expect(useGenerationDraftStore.getState().presetId).toBe('_profile');
});

test('saved profile effects refresh an automatic selection while an explicit override survives', () => {
  profiles = profiles.map((p) => ({ ...p, effects_chain: [] }));
  mount();
  expect(useGenerationDraftStore.getState().presetId).toBeNull();
  act(() => renderer.unmount());
  profiles = profiles.map((p) => ({ ...p, effects_chain: [{ type: 'new default' }] }));
  mount();
  expect(useGenerationDraftStore.getState().presetId).toBe('_profile');
  chooseEffects('none');
  profiles = profiles.map((p) => ({ ...p, effects_chain: [{ type: 'another default' }] }));
  rerender();
  expect(useGenerationDraftStore.getState().presetId).toBeNull();
  chooseEffects('custom');
  profiles = profiles.map((p) => ({ ...p, effects_chain: [] }));
  rerender();
  expect(useGenerationDraftStore.getState().presetId).toBe('custom');
});

test('an explicit None sends an empty chain so backend profile defaults cannot be reapplied', async () => {
  mount();
  chooseEffects('none');
  type('A take without the voice effects.');
  await act(async () => {
    await renderer.root.findByType('form').props.onSubmit({ preventDefault() {}, persist() {} });
  });
  expect(requests[0]).toHaveProperty('effects_chain', []);
});

test('automatic voice defaults wait for profile data instead of silently disabling effects', async () => {
  hiddenProfile = true;
  mount();
  type('Wait for this voice to finish loading.');
  const submit = () =>
    renderer.root.findByType('form').props.onSubmit({ preventDefault() {}, persist() {} });
  await act(async () => {
    await submit();
  });
  expect(requests).toHaveLength(0);
  hiddenProfile = false;
  rerender();
  await act(async () => {
    await submit();
  });
  expect(requests[0]).toHaveProperty('effects_chain', profiles[0].effects_chain);
});

test('compose discards stale results after edits, profile changes, and unmount', async () => {
  mount();
  let resolve!: (value: { text: string }) => void;
  compose = () =>
    new Promise((r) => {
      resolve = r;
    });
  const click = () =>
    renderer.root.findByProps({ 'aria-label': 'generation.compose.ariaLabel' }).props.onClick();
  let pending!: Promise<void>;
  act(() => {
    pending = click();
  });
  type('typed after compose');
  await act(async () => {
    resolve({ text: 'stale edit' });
    await pending;
  });
  expect(renderer.root.findByType('textarea').props.value).toBe('typed after compose');
  act(() => {
    pending = click();
    ui.getState().setSelectedProfileId('b');
  });
  await act(async () => {
    resolve({ text: 'stale profile' });
    await pending;
  });
  expect(renderer.root.findByType('textarea').props.value).toBe('typed after compose');
  act(() => {
    pending = click();
    ui.getState().setSelectedProfileId('a');
    ui.getState().setSelectedProfileId('b');
  });
  await act(async () => {
    resolve({ text: 'stale round trip' });
    await pending;
  });
  expect(renderer.root.findByType('textarea').props.value).toBe('typed after compose');
  act(() => {
    pending = click();
    renderer.unmount();
  });
  await act(async () => {
    resolve({ text: 'stale unmount' });
    await pending;
  });
  mount();
  expect(renderer.root.findByType('textarea').props.value).toBe('typed after compose');
});

test('submission captures profile and effects before the model check', async () => {
  let release!: (value: { models: never[] }) => void;
  status = new Promise((resolve) => {
    release = resolve;
  });
  mount();
  chooseEffects('custom');
  type('first take');
  let pending!: Promise<void>;
  await act(async () => {
    pending = renderer.root
      .findByType('form')
      .props.onSubmit({ preventDefault() {}, persist() {} });
    await Promise.resolve();
  });
  act(() => {
    ui.getState().setSelectedProfileId('b');
    chooseEffects('none');
    type('next take');
  });
  await act(async () => {
    release({ models: [] });
    await pending;
  });
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({
    profile_id: 'a',
    text: 'first take',
    effects_chain: [{ type: 'custom' }],
  });
  expect(renderer.root.findByType('textarea').props.value).toBe('next take');
});

test('docked and floating modes share a form and scope the submit shortcut to it', () => {
  act(() => {
    renderer = create(<FloatingGenerateBox layout="docked" />);
  });
  expect(renderer.root.findByProps({ 'data-composer-layout': 'docked' })).toBeTruthy();
  const form = renderer.root.findByType('form');
  let submissions = 0;
  let prevented = false;
  act(() =>
    form.props.onKeyDown({
      key: 'Enter',
      metaKey: true,
      ctrlKey: false,
      defaultPrevented: false,
      nativeEvent: { isComposing: false },
      preventDefault() {
        prevented = true;
      },
      target: {},
      currentTarget: {
        contains: () => true,
        requestSubmit() {
          submissions++;
        },
      },
    }),
  );
  expect(prevented).toBe(true);
  expect(submissions).toBe(1);
  act(() => renderer.update(<FloatingGenerateBox layout="floating" />));
  const floating = renderer.root.findByProps({ 'data-composer-layout': 'floating' });
  expect(floating.props.className).toContain('fixed');
  expect(floating.props.style.left).toContain('var(--app-rail-width)');
  expect(floating.props.style.bottom).toContain('var(--app-player-height)');
});

test('shortcut dispatches one request and ignores repeated and composing keys', async () => {
  mount();
  type('A line');
  let release!: (value: { models: never[] }) => void;
  status = new Promise((resolve) => {
    release = resolve;
  });
  const form = renderer.root.findByType('form');
  const submits: Promise<void>[] = [];
  const key = (overrides: Record<string, unknown> = {}) => ({
    key: 'Enter',
    metaKey: false,
    ctrlKey: true,
    repeat: false,
    defaultPrevented: false,
    nativeEvent: { isComposing: false },
    preventDefault() {},
    target: {},
    currentTarget: {
      contains: () => true,
      requestSubmit() {
        submits.push(form.props.onSubmit({ preventDefault() {}, persist() {} }));
      },
    },
    ...overrides,
  });
  await act(async () => {
    form.props.onKeyDown(key());
    form.props.onKeyDown(key());
    form.props.onKeyDown(
      key({
        currentTarget: {
          contains: () => false,
          requestSubmit() {
            throw new Error('portal submitted');
          },
        },
      }),
    );
    form.props.onKeyDown(key({ repeat: true }));
    form.props.onKeyDown(key({ nativeEvent: { isComposing: true } }));
    await Promise.resolve();
  });
  expect(submits).toHaveLength(2);
  await act(async () => {
    release({ models: [] });
    await Promise.all(submits);
  });
  expect(requests).toHaveLength(1);
  expect(requests[0].text).toBe('A line');
});

test('failed generation preserves the draft and permits a successful retry', async () => {
  generationError = new Error('Transport unavailable');
  mount();
  type('Keep this draft');
  const submit = () =>
    renderer.root.findByType('form').props.onSubmit({ preventDefault() {}, persist() {} });
  await act(async () => {
    await submit();
  });
  expect(requests).toHaveLength(1);
  expect(renderer.root.findByType('textarea').props.value).toBe('Keep this draft');
  expect(useGenerationDraftStore.getState().submitting).toBe(false);
  expect(toasts).toContainEqual(
    expect.objectContaining({
      title: 'Generation failed',
      description: 'Transport unavailable',
      variant: 'destructive',
    }),
  );
  generationError = undefined;
  await act(async () => {
    await submit();
  });
  expect(requests).toHaveLength(2);
  expect(requests[1].text).toBe('Keep this draft');
  expect(renderer.root.findByType('textarea').props.value).toBe('');
});

test('empty and overlong text are rejected before model lookup and generation', async () => {
  mount();
  const submit = () =>
    renderer.root.findByType('form').props.onSubmit({ preventDefault() {}, persist() {} });
  await act(async () => {
    await submit();
  });
  type('x'.repeat(50001));
  await act(async () => {
    await submit();
  });
  expect(requests).toHaveLength(0);
  expect(statusCalls).toBe(0);
  expect(useGenerationDraftStore.getState().submitting).toBe(false);
  expect(renderer.root.findByType('textarea').props.value).toHaveLength(50001);
});
