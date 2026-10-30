import { afterAll, expect, mock, test } from 'bun:test';
import { act, create } from 'react-test-renderer';
import { parseChangelog } from '../src/lib/utils/parseChangelog';

const raw = `# Changelog

## [Unreleased]
### Added
- **Safe** audio with \`waves\` and [guide](https://example.com/guide)
- Another item
#### Details
A paragraph.
| Feature | State |
| --- | --- |
| Export | Ready |
line 1
line 2
line 3
line 4
line 5

## [1.0.0] - 2026-10-29
### Fixed
- A short release

[1.0.0]: https://example.com/releases/1.0.0
`;
mock.module('virtual:changelog', () => ({ default: raw }));
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
const { ChangelogPage } = await import('../src/components/ServerTab/ChangelogPage');
afterAll(() => mock.restore());

test('renders release sections, formatted text, tables, and safe external links', () => {
  let view!: ReturnType<typeof create>;
  act(() => { view = create(<ChangelogPage />); });
  expect(view.root.findAllByType('h3').map((node) => node.children.join(''))).toEqual(['Unreleased', '1.0.0']);
  expect(view.root.findAllByType('strong').map((node) => node.children.join(''))).toContain('Safe');
  expect(view.root.findAllByType('code').map((node) => node.children.join(''))).toContain('waves');
  expect(view.root.findByType('a').props).toMatchObject({ href: 'https://example.com/guide', target: '_blank', rel: 'noopener noreferrer' });
  expect(view.root.findAllByType('th').map((node) => node.children.join(''))).toEqual(['Feature', 'State']);
  expect(view.root.findAllByType('td').map((node) => node.children.join(''))).toEqual(['Export', 'Ready']);
  expect(JSON.stringify(view.toJSON())).toContain('settings.changelog.devBadge');
  expect(JSON.stringify(view.toJSON())).toContain('2026-10-29');
  act(() => view.unmount());
});

test('long releases expand and collapse without affecting short releases', () => {
  let view!: ReturnType<typeof create>;
  act(() => { view = create(<ChangelogPage />); });
  const toggle = () => view.root.findAllByType('button')[0];
  expect(toggle().children).toContain('settings.changelog.showMore');
  act(() => toggle().props.onClick());
  expect(toggle().children).toContain('settings.changelog.showLess');
  act(() => toggle().props.onClick());
  expect(toggle().children).toContain('settings.changelog.showMore');
  expect(view.root.findAllByType('button')).toHaveLength(1);
  act(() => view.unmount());
});

test('parses Windows line endings and strips prerelease link references', () => {
  const input = '# Changelog\r\n\r\n## [Unreleased]\r\n- Next\r\n\r\n## [1.0.0-beta.1] - 2026-10-29\r\n- Fix\r\n\r\n[1.0.0-beta.1]: https://example.com/beta\r\n';
  expect(parseChangelog(input)).toEqual([
    { version: 'Unreleased', date: null, body: '- Next' },
    { version: '1.0.0-beta.1', date: '2026-10-29', body: '- Fix' },
  ]);
});

test('a list directly after a heading remains content rather than becoming a date', () => {
  expect(parseChangelog('## [2.0]\n- First change\n- Second change')).toEqual([
    { version: '2.0', date: null, body: '- First change\n- Second change' },
  ]);
});

test('unrecognized text without version headings produces no entries', () => {
  expect(parseChangelog('# Changelog\nNothing released yet')).toEqual([]);
});
