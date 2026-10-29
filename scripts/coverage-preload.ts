import { afterAll } from 'bun:test';
import { writeFileSync } from 'node:fs';
import { resolve, sep } from 'node:path';

const toolsRoot = process.env.VOICEBOX_COVERAGE_TOOLS;
const root = process.env.VOICEBOX_COVERAGE_SOURCE;
const output = process.env.VOICEBOX_COVERAGE_FILE;
if (!toolsRoot || !root || !output)
  throw new Error('Coverage instrumentation requires explicit paths');
const { createInstrumenter } = require(resolve(toolsRoot, 'node_modules/istanbul-lib-instrument'));
const sourcePrefix = resolve(root, 'app/src') + sep;
const sourcePattern = new RegExp(
  `^${sourcePrefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}.*\\.[jt]sx?$`,
);

Bun.plugin({
  name: 'voicebox-source-coverage',
  setup(build) {
    build.onLoad({ filter: sourcePattern }, async ({ path }) => {
      const instrumenter = createInstrumenter({
        esModules: true,
        compact: false,
        parserPlugins: ['typescript', 'jsx'],
        coverageGlobalScope: 'globalThis',
        coverageGlobalScopeFunc: false,
      });
      return {
        contents: instrumenter.instrumentSync(await Bun.file(path).text(), path),
        loader: path.endsWith('tsx') ? 'tsx' : 'ts',
      };
    });
  },
});

function flushCoverage() {
  const data = (globalThis as typeof globalThis & { __coverage__?: unknown }).__coverage__;
  writeFileSync(output, JSON.stringify(data ?? {}));
}
// Bun's test command does not consistently emit Node's process exit event.
afterAll(flushCoverage);
process.on('exit', flushCoverage);
