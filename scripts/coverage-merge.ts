import { readdirSync, readFileSync } from 'node:fs';
import { relative, resolve, sep } from 'node:path';

const [root, runs, toolsRoot] = process.argv.slice(2);
if (!root || !runs || !toolsRoot)
  throw new Error('Expected source, report and tooling directories');
const { createCoverageMap } = require(resolve(toolsRoot, 'node_modules/istanbul-lib-coverage'));
const { createInstrumenter } = require(resolve(toolsRoot, 'node_modules/istanbul-lib-instrument'));
const map = createCoverageMap({});
const sourceRoot = resolve(root, 'app/src');
const files: string[] = [];
function visit(dir: string) {
  for (const item of readdirSync(dir, { withFileTypes: true })) {
    const path = resolve(dir, item.name);
    if (item.isDirectory()) visit(path);
    else if (/\.tsx?$/.test(item.name)) files.push(path);
  }
}
visit(sourceRoot);
// Seed every production module with zero counters. Missing imports therefore
// remain uncovered, instead of disappearing from the overall denominator.
for (const file of files) {
  const instrumenter = createInstrumenter({
    esModules: true,
    parserPlugins: ['typescript', 'jsx'],
  });
  instrumenter.instrumentSync(readFileSync(file, 'utf8'), file);
  map.addFileCoverage(instrumenter.lastFileCoverage());
}
const imported = new Set<string>();
for (const item of readdirSync(runs)) {
  if (!item.endsWith('.coverage.json')) continue;
  const data = JSON.parse(readFileSync(resolve(runs, item), 'utf8'));
  for (const path of Object.keys(data)) {
    if (path.startsWith(sourceRoot + sep)) {
      imported.add(path);
      map.merge({ [path]: data[path] });
    }
  }
}
const measured = createCoverageMap({});
for (const path of imported) measured.addFileCoverage(map.fileCoverageFor(path));
const rows = files.sort().map((path) => {
  const entry = map.fileCoverageFor(path);
  const lineHits = entry.getLineCoverage();
  return {
    file: relative(root, path).split(sep).join('/'),
    imported: imported.has(path),
    ...entry.toSummary().toJSON(),
    uncoveredLines: Object.entries(lineHits)
      .filter(([, hits]) => hits === 0)
      .map(([n]) => Number(n)),
  };
});
process.stdout.write(
  JSON.stringify({
    engine: 'Istanbul 6.0.3 instrumentation / coverage 3.2.2',
    allFrontend: map.getCoverageSummary().toJSON(),
    importedModules: measured.getCoverageSummary().toJSON(),
    importedSourceFiles: imported.size,
    allSourceFiles: files.length,
    unimportedSourceFiles: rows.filter((row) => !row.imported).map((row) => row.file),
    scope:
      'All app/src TypeScript modules, including zero counters for modules not imported by tests',
    files: rows,
  }),
);
