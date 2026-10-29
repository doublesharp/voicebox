import { expect, test } from 'bun:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { HelpButton } from '../src/components/ui/HelpButton';

test('help has a named, non-submitting popover trigger', () => {
  const html = renderToStaticMarkup(
    <form>
      <HelpButton label="About checkpoints">A saved version.</HelpButton>
    </form>,
  );
  expect(html).toContain('aria-label="About checkpoints"');
  expect(html).toContain('type="button"');
  expect(html).toContain('aria-haspopup="dialog"');
  expect(html).toContain('aria-expanded="false"');
});
