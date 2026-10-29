import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const profiles = {
  a: {
    id: 'a',
    name: 'Alpha',
    description: 'A',
    language: 'en',
    effects_chain: [{ type: 'gain', enabled: true, params: { gain_db: 1 } }],
  },
  b: { id: 'b', name: 'Beta', description: 'B', language: 'en', effects_chain: [] },
};
let currentProfiles = { ...profiles };
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
let update = deferred();
const effectCalls: unknown[] = [];
mock.module('../src/lib/hooks/useProfiles', () => ({
  useProfile: (id: 'a' | 'b') => ({ data: currentProfiles[id] }),
  useUpdateProfile: () => ({ mutateAsync: () => update.promise, isPending: false }),
  useUpdateProfileEffects: () => ({
    mutateAsync: async ({
      profileId,
      effectsChain,
    }: {
      profileId: string;
      effectsChain: unknown;
    }) => {
      effectCalls.push([profileId, effectsChain]);
    },
  }),
  useUploadAvatar: () => ({ mutate() {} }),
  useDeleteAvatar: () => ({ mutateAsync: async () => {}, isPending: false }),
}));
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    updateProfileEffects: async (...args: unknown[]) => {
      effectCalls.push(args);
    },
  },
}));
mock.module('../src/components/Effects/EffectsChainEditor', () => ({
  EffectsChainEditor: (props: object) => <chain-editor {...props} />,
}));
mock.module('../src/components/VoiceProfiles/SampleList', () => ({ SampleList: () => null }));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast: () => {} }) }));
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
let renderer: ReactTestRenderer;
async function mount(id: 'a' | 'b') {
  await act(async () => {
    renderer = create(<VoiceInspector profileId={id} />);
  });
}
const name = () =>
  renderer.root
    .findAllByType('input')
    .find((n) => n.props.placeholder === 'profileForm.fields.namePlaceholder')!;
const editor = () => renderer.root.findByType('chain-editor' as never);
const subtitle = () => renderer.root.findAllByType('header')[0].findByType('p').children.join('');
const submit = async () => {
  await act(async () => {
    renderer.root.findByType('form').props.onSubmit({ preventDefault() {}, persist() {} });
    await Bun.sleep(10);
  });
};
afterEach(() => {
  act(() => renderer?.unmount());
  currentProfiles = { ...profiles };
  effectCalls.length = 0;
});
afterAll(() => mock.restore());

test('header counts use the selected library summary rather than detail defaults', async () => {
  await mount('a');
  act(() => {
    renderer.update(
      <VoiceInspector
        profileId="a"
        profileSummary={{ id: 'a', sample_count: 2, generation_count: 5 }}
      />,
    );
  });
  expect(subtitle()).toBe('EN · 2 samples · 5 takes');
});

test.each([
  undefined,
  { id: 'b', sample_count: 2, generation_count: 5 },
])('missing or stale summary does not invent zero counts', async (profileSummary) => {
  await mount('a');
  act(() => renderer.update(<VoiceInspector profileId="a" profileSummary={profileSummary} />));
  expect(subtitle()).toBe('EN');
});

test('profile refetch preserves unsaved fields and effects', async () => {
  await mount('a');
  await act(async () => {
    await name().props.onChange({ target: { name: 'name', value: 'Draft' }, type: 'change' });
  });
  const draftChain = [{ type: 'gain', enabled: true, params: { gain_db: 7 } }];
  act(() => editor().props.onChange(draftChain));
  currentProfiles.a = { ...profiles.a, description: 'refetched' };
  await act(async () => {
    renderer.update(<VoiceInspector profileId="a" />);
  });
  expect(name().props.value).toBe('Draft');
  expect(editor().props.value).toEqual(draftChain);
});

test('old profile save completion cannot clear new profile effects', async () => {
  update = deferred();
  await mount('a');
  act(() => editor().props.onChange([{ type: 'gain', enabled: true, params: { gain_db: 3 } }]));
  await submit();
  await act(async () => {
    renderer.update(<VoiceInspector profileId="b" />);
  });
  const draft = [{ type: 'gain', enabled: true, params: { gain_db: 9 } }];
  act(() => editor().props.onChange(draft));
  await act(async () => {
    update.resolve();
    await update.promise;
  });
  expect(editor().props.value).toEqual(draft);
  expect(
    renderer.root
      .findAllByType('button')
      .some((n) => n.children.includes('common.save') && !n.props.disabled),
  ).toBe(true);
  expect(effectCalls[0]?.[0]).toBe('a');
});

test('a late metadata refetch cannot restore effects that were just saved', async () => {
  update = deferred();
  await mount('a');
  const saved = [{ type: 'gain', enabled: true, params: { gain_db: 8 } }];
  act(() => editor().props.onChange(saved));
  await submit();
  await act(async () => {
    update.resolve();
    await update.promise;
  });
  currentProfiles.a = { ...profiles.a, description: 'metadata refetch' };
  await act(async () => {
    renderer.update(<VoiceInspector profileId="a" />);
  });
  expect(editor().props.value).toEqual(saved);
});

test('effects edited while a save is pending remain dirty after completion', async () => {
  update = deferred();
  await mount('a');
  act(() => editor().props.onChange([{ type: 'gain', enabled: true, params: { gain_db: 3 } }]));
  await submit();
  const later = [{ type: 'gain', enabled: true, params: { gain_db: 10 } }];
  act(() => editor().props.onChange(later));
  await act(async () => {
    update.resolve();
    await update.promise;
  });
  expect(editor().props.value).toEqual(later);
  expect(
    renderer.root
      .findAllByType('button')
      .some((n) => n.children.includes('common.save') && !n.props.disabled),
  ).toBe(true);
});

test('a stale metadata refetch cannot restore a name that was just saved', async () => {
  update = deferred();
  await mount('a');
  await act(async () => {
    await name().props.onChange({ target: { name: 'name', value: 'Saved name' }, type: 'change' });
  });
  await submit();
  await act(async () => {
    update.resolve();
    await update.promise;
  });
  currentProfiles.a = { ...profiles.a, description: 'old refetch' };
  await act(async () => {
    renderer.update(<VoiceInspector profileId="a" />);
  });
  expect(name().props.value).toBe('Saved name');
});

test('avatar editing has an accessible name and visible keyboard focus', async () => {
  await mount('a');
  const avatar = renderer.root.findByProps({ 'aria-label': 'voiceInspector.changeAvatar' });
  expect(avatar.props.type).toBe('button');
  expect(avatar.props.className).toContain('focus-visible:ring-2');
});

test('the stable save footer targets its form and reflects clean and dirty edits', async () => {
  await mount('a');
  const save = () =>
    renderer.root.findAllByType('button').find((node) => node.children.includes('common.save'))!;
  expect(save().props.disabled).toBe(true);
  expect(save().props.form).toBe(renderer.root.findByType('form').props.id);
  expect(renderer.root.findByType('h2').children).toEqual(['Alpha']);
  await act(async () => {
    await name().props.onChange({ target: { name: 'name', value: 'Draft' }, type: 'change' });
  });
  expect(save().props.disabled).toBe(false);
  const discard = renderer.root
    .findAllByType('button')
    .find((node) => node.children.includes('voiceInspector.discard'))!;
  act(() => discard.props.onClick());
  expect(name().props.value).toBe('Alpha');
  expect(save().props.disabled).toBe(true);
});
