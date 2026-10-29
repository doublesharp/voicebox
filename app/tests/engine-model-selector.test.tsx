import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { createElement, type ReactNode } from 'react';
import { useForm } from 'react-hook-form';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const pass = ({ children }: { children: ReactNode }) => <>{children}</>;
mock.module('../src/components/ui/form', () => ({ FormControl: pass }));
mock.module('../src/components/ui/select', () => ({
  Select: (props: any) => createElement('selector', props),
  SelectContent: pass,
  SelectItem: (props: any) => createElement('option', props),
  SelectTrigger: pass,
  SelectValue: () => null,
}));
const { EngineModelSelector, isProfileCompatibleWithEngine } = await import(
  '../src/components/Generation/EngineModelSelector'
);
let form: any;
let view: ReactTestRenderer;
function Harness({ profile, engine = 'qwen', size = '1.7B', language = 'fr' }: any) {
  form = useForm({ defaultValues: { engine, modelSize: size, language } });
  return <EngineModelSelector form={form} selectedProfile={profile} />;
}
function mount(props: any = {}) {
  act(() => {
    view = create(<Harness {...props} />);
  });
}
function values() {
  return view.root.findAllByType('option').map((item) => item.props.value);
}
function choose(value: string) {
  act(() => view.root.findByType('selector' as never).props.onValueChange(value));
}
afterEach(() => {
  if (view) act(() => view.unmount());
});
afterAll(() => mock.restore());

test('voice types restrict the offered engines and preserve compatible selection', () => {
  mount({
    profile: { voice_type: 'preset', preset_engine: 'qwen_custom_voice' },
    engine: 'qwen_custom_voice',
    size: '0.6B',
  });
  expect(values()).toEqual(['qwen_custom_voice:1.7B', 'qwen_custom_voice:0.6B']);
  expect(form.getValues('modelSize')).toBe('0.6B');
  expect(
    isProfileCompatibleWithEngine({ voice_type: 'designed' } as any, 'qwen_voice_design'),
  ).toBe(true);
  expect(isProfileCompatibleWithEngine({ voice_type: 'cloned' } as any, 'kokoro')).toBe(false);
});

test('unknown legacy voice types follow cloned-voice engine restrictions', () => {
  expect(isProfileCompatibleWithEngine({ voice_type: 'legacy' } as any, 'kokoro')).toBe(false);
  expect(isProfileCompatibleWithEngine({ voice_type: 'legacy' } as any, 'qwen')).toBe(true);
});

test('a profile change switches an incompatible engine to a compatible one', () => {
  mount({ profile: { voice_type: 'cloned' }, engine: 'qwen' });
  expect(values()).not.toContain('kokoro');
  act(() => view.update(<Harness profile={{ voice_type: 'preset', preset_engine: 'kokoro' }} />));
  expect(form.getValues('engine')).toBe('kokoro');
  expect(values()).toEqual(['kokoro']);
});

test('an invalid size falls back within the selected engine', () => {
  mount({ profile: { voice_type: 'cloned' }, engine: 'tada', size: '1.7B' });
  expect(form.getValues('engine')).toBe('tada');
  expect(form.getValues('modelSize')).toBe('1B');
});

test('an externally selected English-only model corrects an incompatible saved language', () => {
  mount({ engine: 'tada', size: '1B', language: 'fr' });
  expect(form.getValues()).toMatchObject({ engine: 'tada', modelSize: '1B', language: 'en' });
});

test('engine and model selection preserve supported languages and reset unsupported ones', () => {
  mount({ language: 'fr' });
  choose('tada:3B');
  expect(form.getValues()).toMatchObject({ engine: 'tada', modelSize: '3B', language: 'fr' });
  choose('tada:1B');
  expect(form.getValues()).toMatchObject({ engine: 'tada', modelSize: '1B', language: 'en' });
  choose('qwen_custom_voice:0.6B');
  expect(form.getValues()).toMatchObject({
    engine: 'qwen_custom_voice',
    modelSize: '0.6B',
    language: 'en',
  });
  choose('chatterbox_turbo');
  expect(form.getValues()).toMatchObject({ engine: 'chatterbox_turbo', language: 'en' });
});
