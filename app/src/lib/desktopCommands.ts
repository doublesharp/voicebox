const destinations = new Set([
  '/studio/projects',
  '/',
  '/voices',
  '/captures',
  '/effects',
  '/models',
  '/stories',
  '/settings',
]);

/** Route native menu requests through the same router and its leave guards as the UI. */
export function attachDesktopCommands(
  target: Pick<Window, 'addEventListener' | 'removeEventListener'>,
  navigate: (path: string) => void,
  toggleSidebar: () => void,
): () => void {
  const open = (event: Event) => {
    const path = (event as CustomEvent<unknown>).detail;
    if (typeof path === 'string' && destinations.has(path)) navigate(path);
  };
  target.addEventListener('voicebox:menu-navigate', open);
  target.addEventListener('voicebox:toggle-sidebar', toggleSidebar);
  return () => {
    target.removeEventListener('voicebox:menu-navigate', open);
    target.removeEventListener('voicebox:toggle-sidebar', toggleSidebar);
  };
}
