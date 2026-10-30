import { useServerStore } from '@/stores/serverStore';

/** Keep one native preference write in flight across startup and settings views. */
export async function syncKeepServerRunning(
  checked: boolean,
  apply: (value: boolean) => Promise<void>,
): Promise<boolean> {
  const state = useServerStore.getState();
  if (state.keepServerRunningSyncing) return false;
  const previous = state.keepServerRunningOnClose;
  useServerStore.setState({ keepServerRunningSyncing: true, keepServerRunningOnClose: checked });
  try {
    await apply(checked);
    return true;
  } catch (error) {
    useServerStore.setState({ keepServerRunningOnClose: previous });
    throw error;
  } finally {
    useServerStore.setState({ keepServerRunningSyncing: false });
  }
}
