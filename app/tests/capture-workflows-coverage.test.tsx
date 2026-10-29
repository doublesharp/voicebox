import { afterAll, expect, mock, test } from 'bun:test';
import { act, create } from 'react-test-renderer';

mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
const { CapturePill } = await import('../src/components/CapturePill/CapturePill');
const originalNavigator = globalThis.navigator;
afterAll(() => {
  globalThis.navigator = originalNavigator;
  mock.restore();
});

test('pill renders state, elapsed time, fallback microphone and stop action', () => {
  const stop = mock(() => {});
  const view = create(
    <CapturePill state="recording" elapsedMs={61000} usingDefaultMicrophone onStop={stop} />,
  );
  expect(view.root.findByType('output').children).toEqual(['microphone.recordingDefault']);
  expect(view.root.findAllByType('span').some((n) => n.children.includes('1:01'))).toBe(true);
  act(() => view.root.findByProps({ 'aria-label': 'captures.pill.stopAria' }).props.onClick());
  expect(stop).toHaveBeenCalledTimes(1);
  act(() => view.update(<CapturePill state="rest" elapsedMs={-1} onStop={stop} />));
  expect(view.root.findAllByType('button')).toHaveLength(0);
  expect(view.root.findAllByType('span').some((n) => n.children.includes('0:00'))).toBe(true);
  view.unmount();
});

test('error pill dismisses even when clipboard is denied', async () => {
  const dismiss = mock(() => {});
  globalThis.navigator = {
    clipboard: {
      writeText: async () => {
        throw new Error('denied');
      },
    },
  } as Navigator;
  const view = create(
    <CapturePill state="error" elapsedMs={0} errorMessage="failure" onDismiss={dismiss} />,
  );
  expect(view.root.findByType('button').children.some((child) => typeof child !== 'string')).toBe(
    true,
  );
  await act(async () => view.root.findByType('button').props.onClick());
  expect(dismiss).toHaveBeenCalledTimes(1);
  view.unmount();
});
