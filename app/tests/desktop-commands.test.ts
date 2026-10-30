import { expect, test } from 'bun:test';
import { attachDesktopCommands } from '../src/lib/desktopCommands';

test('native menu requests route only known destinations and clean up their listeners', () => {
  const target = new EventTarget();
  const paths: string[] = [];
  let toggles = 0;
  const dispose = attachDesktopCommands(
    target,
    (path) => paths.push(path),
    () => toggles++,
  );
  target.dispatchEvent(new CustomEvent('voicebox:menu-navigate', { detail: '/voices' }));
  target.dispatchEvent(new CustomEvent('voicebox:menu-navigate', { detail: '/settings' }));
  target.dispatchEvent(
    new CustomEvent('voicebox:menu-navigate', { detail: 'https://elsewhere.invalid' }),
  );
  target.dispatchEvent(new Event('voicebox:toggle-sidebar'));
  expect(paths).toEqual(['/voices', '/settings']);
  expect(toggles).toBe(1);
  dispose();
  target.dispatchEvent(new CustomEvent('voicebox:menu-navigate', { detail: '/voices' }));
  target.dispatchEvent(new Event('voicebox:toggle-sidebar'));
  expect(paths).toEqual(['/voices', '/settings']);
  expect(toggles).toBe(1);
});
