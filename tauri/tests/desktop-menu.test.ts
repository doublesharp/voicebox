import { expect, test } from 'bun:test';
import { desktopMenuOptions } from '../src/platform/desktopMenu';

test('desktop menus expose navigation and native editing without taking Studio undo shortcuts', () => {
  for (const mac of [true, false]) {
    const sent: string[] = [];
    const menu = desktopMenuOptions(mac, (action) => sent.push(action));
    const sections = menu.items as Array<{
      text: string;
      items: Array<{ item?: string; text?: string; action?: () => void; accelerator?: string }>;
    }>;
    expect(sections.map((section) => section.text)).toEqual(
      mac ? ['Voicebox DS', 'File', 'Edit', 'View', 'Help'] : ['File', 'Edit', 'View', 'Help'],
    );
    const edit = sections.find((section) => section.text === 'Edit')!;
    expect(edit.items.map((item) => item.item)).toEqual(['Cut', 'Copy', 'Paste', 'SelectAll']);
    const view = sections.find((section) => section.text === 'View')!;
    view.items.find((item) => item.text === 'Toggle Sidebar')!.action!();
    view.items.find((item) => item.text === 'Voices')!.action!();
    sections.find((section) => section.text === 'Help')!.items[0].action!();
    expect(sent).toEqual(['toggle-sidebar', 'navigate:/voices', 'keyboard-shortcuts']);
    expect(view.items.filter((item) => item.action).every((item) => !item.accelerator)).toBe(true);
  }
});
