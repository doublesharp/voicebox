import { afterAll, expect, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
const values = new Map([['voicebox:lang', 'es']]);
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: {
    localStorage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    },
  },
});
const { default: i18n, i18nReady } = await import('../src/i18n');
const { LocaleReady } = await import('../src/i18n/LocaleReady');

afterAll(() => {
  if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

test('saved locale is ready before the first translated child renders', async () => {
  const rendered: string[] = [];
  function Translated() {
    const text = i18n.t('common.cancel');
    rendered.push(text);
    return <span>{text}</span>;
  }
  let tree!: ReactTestRenderer;
  await act(async () => {
    tree = create(
      <LocaleReady>
        <Translated />
      </LocaleReady>,
    );
    await i18nReady;
  });
  expect(rendered.length).toBeGreaterThan(0);
  expect(rendered.every((text) => text === 'Cancelar')).toBe(true);
  expect(i18n.hasResourceBundle('ja', 'translation')).toBe(false);
  act(() => tree.unmount());
});
