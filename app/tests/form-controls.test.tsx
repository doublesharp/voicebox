import { expect, test } from 'bun:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { Button } from '../src/components/ui/button';

test('secondary actions inside forms never implicitly submit them', () => {
  const html = renderToStaticMarkup(
    <form>
      <Button>Clear effects</Button>
      <Button type="submit">Save changes</Button>
    </form>,
  );
  const buttons = [...html.matchAll(/<button\b[^>]*>/g)].map(([tag]) => tag);
  expect(buttons[0]).toContain('type="button"');
  expect(buttons[1]).toContain('type="submit"');
});

test('button styling on links preserves native link semantics', () => {
  const html = renderToStaticMarkup(
    <Button asChild>
      <a href="/settings">Settings</a>
    </Button>,
  );
  expect(html).toContain('<a ');
  expect(html).not.toContain('type=');
});
