import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { UpdateStatus } from '../src/platform/types';

const initial: UpdateStatus = {
  checking: false,
  available: false,
  downloading: false,
  installing: false,
  readyToInstall: false,
};
let listener: ((status: UpdateStatus) => void) | undefined;
const unsubscribe = mock(() => {
  listener = undefined;
});
const check = mock(async () => {});
const download = mock(async () => {});
const restart = mock(async () => {});
const update = mock((_options: any) => {});
const toast = mock((_options: any) => ({ id: 'update-toast', update }));
const updater = {
  getStatus: () => initial,
  subscribe: (callback: (status: UpdateStatus) => void) => {
    listener = callback;
    return unsubscribe;
  },
  checkForUpdates: check,
  downloadAndInstall: download,
  restartAndInstall: restart,
};
mock.module('../src/platform/PlatformContext', () => ({
  usePlatform: () => ({ updater, metadata: { isTauri: true } }),
}));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
mock.module('../src/components/ui/progress', () => ({ Progress: () => null }));
mock.module('../src/components/ui/toast', () => ({ ToastAction: () => null }));
const { useAutoUpdater } = await import('../src/hooks/useAutoUpdater');
let view: ReactTestRenderer | undefined;
function Probe({ showToast = true }: { showToast?: boolean }) {
  useAutoUpdater({ checkOnMount: true, showToast });
  return null;
}
beforeEach(() => {
  toast.mockClear();
  update.mockClear();
  check.mockClear();
  download.mockClear();
  restart.mockClear();
  unsubscribe.mockClear();
});
afterEach(() => {
  if (view) act(() => view?.unmount());
  view = undefined;
});
afterAll(() => mock.restore());

test('notifies when an update is available and offers download', async () => {
  await act(async () => {
    view = create(<Probe />);
  });
  expect(check).toHaveBeenCalledTimes(1);
  act(() => listener?.({ ...initial, available: true, version: '2.1.0' }));
  expect(toast).toHaveBeenCalledTimes(1);
  const notification = toast.mock.calls[0][0];
  expect(notification.title).toBe('Update Available');
  expect(notification.description).toContain('2.1.0');
  await act(async () => {
    await notification.action.props.onClick();
  });
  expect(download).toHaveBeenCalledTimes(1);
});

test('updates the notification while downloading and offers restart when ready', async () => {
  await act(async () => {
    view = create(<Probe />);
  });
  act(() => listener?.({ ...initial, available: true, version: '2.1.0' }));
  act(() =>
    listener?.({
      ...initial,
      available: true,
      downloading: true,
      version: '2.1.0',
      downloadProgress: 50,
      downloadedBytes: 1024,
      totalBytes: 2048,
    }),
  );
  expect(update).toHaveBeenCalledTimes(1);
  expect(update.mock.calls[0][0].duration).toBe(Infinity);
  act(() => listener?.({ ...initial, available: true, readyToInstall: true, version: '2.1.0' }));
  const ready = update.mock.calls[1][0];
  expect(ready.title).toBe('Update Ready');
  await act(async () => {
    await ready.action.props.onClick();
  });
  expect(restart).toHaveBeenCalledTimes(1);
});

test('does not display notifications when disabled and unsubscribes on unmount', async () => {
  await act(async () => {
    view = create(<Probe showToast={false} />);
  });
  act(() => listener?.({ ...initial, available: true, version: '2.1.0' }));
  expect(toast).not.toHaveBeenCalled();
  act(() => view?.unmount());
  view = undefined;
  expect(unsubscribe).toHaveBeenCalledTimes(1);
});
