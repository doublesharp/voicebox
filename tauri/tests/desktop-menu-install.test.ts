import { expect, mock, test } from 'bun:test';

let options: {
  items: Array<{ text: string; items: Array<{ text?: string; action?: () => void }> }>;
};
let installed = 0;
mock.module('@tauri-apps/api/menu', () => ({
  Menu: {
    new: async (value: typeof options) => {
      options = value;
      return {
        setAsAppMenu: async () => {
          installed++;
        },
      };
    },
  },
}));
const { installDesktopMenu } = await import('../src/platform/desktopMenu');

test('installing the native menu dispatches commands and installs it only once', async () => {
  const previousWindow = globalThis.window;
  const windowTarget = new EventTarget();
  Object.defineProperty(globalThis, 'window', { value: windowTarget, configurable: true });
  try {
    const paths: string[] = [];
    let toggles = 0;
    windowTarget.addEventListener('voicebox:menu-navigate', (event) =>
      paths.push((event as CustomEvent<string>).detail),
    );
    windowTarget.addEventListener('voicebox:toggle-sidebar', () => toggles++);
    await installDesktopMenu(true);
    await installDesktopMenu(true);
    expect(installed).toBe(1);
    const view = options.items.find((item) => item.text === 'View')!;
    view.items.find((item) => item.text === 'Voices')!.action!();
    view.items.find((item) => item.text === 'Toggle Sidebar')!.action!();
    expect(paths).toEqual(['/voices']);
    expect(toggles).toBe(1);
  } finally {
    Object.defineProperty(globalThis, 'window', { value: previousWindow, configurable: true });
  }
});
