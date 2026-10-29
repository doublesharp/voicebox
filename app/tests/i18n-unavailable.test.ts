import { afterAll, expect, mock, test } from 'bun:test';

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
mock.module('../src/i18n/locales/es/translation.json', () => {
  throw new Error('Locale chunk unavailable');
});
const { default: i18n, i18nReady } = await import('../src/i18n');

afterAll(() => {
  mock.restore();
  if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

test('missing locale chunk completes startup with the English fallback', async () => {
  await i18nReady;
  expect(i18n.isInitialized).toBe(true);
  expect(i18n.t('common.cancel')).toBe('Cancel');
  expect(i18n.hasResourceBundle('ja', 'translation')).toBe(false);
  await expect(i18n.changeLanguage('es')).resolves.toBeFunction();
  expect(i18n.t('common.cancel')).toBe('Cancel');
});
