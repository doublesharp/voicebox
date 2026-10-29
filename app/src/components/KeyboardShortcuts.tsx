import { useNavigate, useRouterState } from '@tanstack/react-router';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { attachAppShortcuts, navigationShortcuts } from '@/lib/keyboardShortcuts';

export function KeyboardShortcuts() {
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const currentPath = useRef(pathname);
  currentPath.current = pathname;
  const [panel, setPanel] = useState<'help' | 'palette' | null>(null);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const focusFrame = useRef<number | null>(null);
  const modifier =
    typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform) ? '⌘' : 'Ctrl';
  const go = useCallback(
    async (path: string, focusScript = false) => {
      setPanel(null);
      setError('');
      try {
        await navigate({ to: path });
        if (focusScript) {
          if (focusFrame.current !== null) cancelAnimationFrame(focusFrame.current);
          focusFrame.current = requestAnimationFrame(() => {
            focusFrame.current = null;
            document.querySelector<HTMLElement>('[aria-label="Script"]')?.focus();
          });
        }
      } catch {
        setError('Could not open that destination. Try again from the sidebar.');
      }
    },
    [navigate],
  );
  useEffect(() => {
    const cleanup = attachAppShortcuts(
      window,
      () => currentPath.current,
      (action) => {
        if (action.type === 'navigate') void go(action.path);
        else if (action.type === 'focus-script') void go('/', true);
        else {
          setQuery('');
          setPanel(action.type);
        }
      },
    );
    const help = () => {
      setQuery('');
      setPanel('help');
    };
    window.addEventListener('voicebox:keyboard-shortcuts', help);
    return () => {
      cleanup();
      window.removeEventListener('voicebox:keyboard-shortcuts', help);
      if (focusFrame.current !== null) cancelAnimationFrame(focusFrame.current);
    };
  }, [go]);
  const common = [
    [`${modifier} K`, 'Open commands (project commands inside Studio)'],
    [`${modifier} /`, 'Show keyboard shortcuts'],
    [`${modifier} ,`, 'Open Settings'],
    [`${modifier} Shift G`, 'Open Generate and focus the script'],
    [`${modifier} Enter`, 'Generate from the focused generation form'],
    ['Space', 'Play / pause when focus is outside a control'],
  ];
  const studio = [
    [`${modifier} S`, 'Save current Studio edits'],
    [`${modifier} Z`, 'Undo project change'],
    [`${modifier} Shift Z / ${modifier} Y`, 'Redo project change'],
    ['S', 'Split selected clip at the playhead (timeline focus)'],
    [`${modifier} D`, 'Duplicate selected clip (timeline focus)'],
    ['Delete / Backspace', 'Delete selected clip (timeline focus)'],
  ];
  return (
    <>
      {error && <p role="alert">{error}</p>}
      <Dialog
        open={panel !== null}
        onOpenChange={(open) => {
          if (!open) setPanel(null);
        }}
      >
        <DialogContent aria-label={panel === 'palette' ? 'App commands' : 'Keyboard shortcuts'}>
          <DialogHeader>
            <DialogTitle>{panel === 'palette' ? 'Go to…' : 'Keyboard shortcuts'}</DialogTitle>
            <DialogDescription>
              {panel === 'palette'
                ? 'Choose a workspace. Use Tab and Enter to select a command.'
                : 'Shortcuts work while the app is focused. Text fields keep their editing keys, and dialogs keep their own controls.'}
            </DialogDescription>
          </DialogHeader>
          {panel === 'palette' ? (
            <>
              <input
                aria-label="Filter app commands"
                className="w-full rounded-md border bg-background px-3 py-2"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
              <nav aria-label="App commands" className="flex flex-col gap-1">
                {[...navigationShortcuts, { path: '/settings', label: 'Settings' }]
                  .filter((item) => item.label.toLowerCase().includes(query.toLowerCase()))
                  .map((item) => (
                    <button
                      key={item.path}
                      type="button"
                      className="rounded-md px-3 py-2 text-left hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
                      onClick={() => void go(item.path)}
                    >
                      {item.label}
                    </button>
                  ))}
              </nav>
            </>
          ) : (
            <>
              <table className="w-full text-sm">
                <caption className="mb-2 text-left font-semibold">Application</caption>
                <tbody>
                  {common.map(([keys, label]) => (
                    <tr key={keys}>
                      <th
                        scope="row"
                        className="py-1 pr-4 text-left font-mono text-xs whitespace-nowrap"
                      >
                        {keys}
                      </th>
                      <td className="py-1">{label}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <table className="w-full text-sm">
                <caption className="mb-2 text-left font-semibold">
                  Navigation · outside text fields
                </caption>
                <tbody>
                  {navigationShortcuts.map((item) => (
                    <tr key={item.path}>
                      <th scope="row" className="py-1 pr-4 text-left font-mono text-xs">
                        {modifier} Alt {item.digit}
                      </th>
                      <td className="py-1">{item.label}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <table className="w-full text-sm">
                <caption className="mb-2 text-left font-semibold">Studio</caption>
                <tbody>
                  {studio.map(([keys, label]) => (
                    <tr key={keys}>
                      <th
                        scope="row"
                        className="py-1 pr-4 text-left font-mono text-xs whitespace-nowrap"
                      >
                        {keys}
                      </th>
                      <td className="py-1">{label}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
