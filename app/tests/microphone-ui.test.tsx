import { describe, expect, test } from 'bun:test';
import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { CapturePill } from '../src/components/CapturePill/CapturePill';
import translation from '../src/i18n/locales/en/translation.json';

const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation } }, initImmediate: false });

function renderPill(
  usingDefaultMicrophone: boolean,
  state: 'recording' | 'transcribing' = 'recording',
) {
  return renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <CapturePill state={state} elapsedMs={1000} usingDefaultMicrophone={usingDefaultMicrophone} />
    </I18nextProvider>,
  );
}

describe('dictation microphone notice', () => {
  test('a fallback recording visibly names the default mic and explains the switch', () => {
    const html = renderPill(true);
    expect(html).toContain('Recording (default mic)');
    expect(html).toContain('The selected microphone is unavailable.');
    expect(html).toContain('<output');
  });

  test('ordinary recording and transcription keep their usual labels', () => {
    expect(renderPill(false)).not.toContain('Recording (default mic)');
    expect(renderPill(true, 'transcribing')).not.toContain('Recording (default mic)');
  });
});
