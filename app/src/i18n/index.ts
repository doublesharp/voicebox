import i18n, { type BackendModule } from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { initReactI18next } from 'react-i18next';
import en from './locales/en/translation.json';

export const SUPPORTED_LANGUAGES = [
  { code: 'en', label: 'English' },
  { code: 'es', label: 'Español' },
  { code: 'pt-BR', label: 'Português (Brasil)' },
  { code: 'ja', label: '日本語' },
  { code: 'ko', label: '한국어' },
  { code: 'zh-CN', label: '简体中文' },
  { code: 'zh-TW', label: '繁體中文' },
  { code: 'fr', label: 'Français' },
  { code: 'it', label: 'Italiano' },
] as const;

export type LanguageCode = (typeof SUPPORTED_LANGUAGES)[number]['code'];

const dictionaries = {
  es: () => import('./locales/es/translation.json'),
  fr: () => import('./locales/fr/translation.json'),
  it: () => import('./locales/it/translation.json'),
  ja: () => import('./locales/ja/translation.json'),
  ko: () => import('./locales/ko/translation.json'),
  'pt-BR': () => import('./locales/pt-BR/translation.json'),
  'zh-CN': () => import('./locales/zh-CN/translation.json'),
  'zh-TW': () => import('./locales/zh-TW/translation.json'),
};

/** i18next waits for this backend before publishing a language change. */
const bundledLocales: BackendModule = {
  type: 'backend',
  init() {},
  read(language, _namespace, callback) {
    const load = dictionaries[language as keyof typeof dictionaries];
    if (!load) {
      callback(null, language === 'en' ? en : {});
      return;
    }
    void load().then(
      (module) => callback(null, module.default),
      // Do not request retries for a missing deployment chunk. English remains
      // available even when a selected locale cannot be loaded while offline.
      (error: unknown) =>
        callback(error instanceof Error ? error : new Error(String(error)), false),
    );
  },
};

export const i18nReady = i18n
  .use(bundledLocales)
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
    },
    partialBundledLanguages: true,
    fallbackLng: 'en',
    supportedLngs: SUPPORTED_LANGUAGES.map((l) => l.code),
    load: 'currentOnly',
    interpolation: { escapeValue: false },
    react: { useSuspense: false },
    detection: {
      order: ['localStorage', 'navigator'],
      lookupLocalStorage: 'voicebox:lang',
      caches: ['localStorage'],
    },
  });

export default i18n;
