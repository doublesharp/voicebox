import { Menu, type MenuOptions } from '@tauri-apps/api/menu';

/** The UI owns navigation; native menus request it through the same router. */
export function desktopMenuOptions(mac: boolean, send: (command: string) => void): MenuOptions {
  const command = (text: string, value: string) => ({ text, action: () => send(value) });
  const appMenu = {
    text: 'Voicebox DS',
    items: [
      { item: { About: null } },
      { item: 'Separator' as const },
      command('Settings…', 'navigate:/settings'),
      { item: 'Separator' as const },
      { item: 'Hide' as const },
      { item: 'HideOthers' as const },
      { item: 'ShowAll' as const },
      { item: 'Separator' as const },
      { item: 'Quit' as const },
    ],
  };
  return {
    items: [
      ...(mac ? [appMenu] : []),
      {
        text: 'File',
        items: [
          command('Projects', 'navigate:/studio/projects'),
          { item: 'Separator' },
          { item: 'CloseWindow' },
        ],
      },
      {
        text: 'Edit',
        items: [{ item: 'Cut' }, { item: 'Copy' }, { item: 'Paste' }, { item: 'SelectAll' }],
      },
      {
        text: 'View',
        items: [
          command('Toggle Sidebar', 'toggle-sidebar'),
          { item: 'Separator' },
          command('Generate', 'navigate:/'),
          command('Voices', 'navigate:/voices'),
          command('Captures', 'navigate:/captures'),
          command('Effects', 'navigate:/effects'),
          command('Models', 'navigate:/models'),
          command('Stories', 'navigate:/stories'),
          ...(!mac
            ? [{ item: 'Separator' as const }, command('Settings…', 'navigate:/settings')]
            : []),
        ],
      },
      { text: 'Help', items: [command('Keyboard Shortcuts', 'keyboard-shortcuts')] },
    ],
  };
}

// Keep the handle alive: menu items and their callback channels outlive setup.
let desktopMenu: Menu | undefined;
export async function installDesktopMenu(mac: boolean): Promise<void> {
  if (desktopMenu) return;
  const menu = await Menu.new(
    desktopMenuOptions(mac, (command) => {
      if (command.startsWith('navigate:')) {
        window.dispatchEvent(
          new CustomEvent('voicebox:menu-navigate', { detail: command.slice(9) }),
        );
      } else {
        window.dispatchEvent(new Event(`voicebox:${command}`));
      }
    }),
  );
  await menu.setAsAppMenu();
  desktopMenu = menu;
}
