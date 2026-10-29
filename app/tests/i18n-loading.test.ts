import { expect, test } from 'bun:test';
import i18n from '../src/i18n';

// Exercise the actual registered backend and bundled dictionaries, not a loader mock.
test('startup retains only the fallback dictionary until another language is selected', async () => {
  await i18n.changeLanguage('en');
  expect(i18n.hasResourceBundle('en', 'translation')).toBe(true);
  expect(i18n.hasResourceBundle('es', 'translation')).toBe(false);
  expect(i18n.hasResourceBundle('ja', 'translation')).toBe(false);
  expect(i18n.t('common.cancel')).toBe('Cancel');

  const switching = i18n.changeLanguage('es');
  expect(i18n.language).toBe('en');
  expect(i18n.t('common.cancel')).toBe('Cancel');
  await switching;
  expect(i18n.hasResourceBundle('es', 'translation')).toBe(true);
  expect(i18n.hasResourceBundle('ja', 'translation')).toBe(false);
  expect(i18n.t('common.cancel')).toBe('Cancelar');
  expect(i18n.t('history.versions.deleteFailed')).toBe('Could not delete version');

  await i18n.changeLanguage('ja');
  expect(i18n.t('common.cancel')).toBe('キャンセル');
  await i18n.changeLanguage('en');
  expect(i18n.t('common.cancel')).toBe('Cancel');
});
