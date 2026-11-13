// Fail the build when the first page load grows past its budget.
//   node web/dev/size-check.mjs <built-web-dir>
// Counts what index.html references directly (entry script, its preloads, stylesheets), gzip-compressed.
// Pages and the topology canvas load on demand and are reported but only loosely limited.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';

const BUDGET = { initialJs: 65_000, initialCss: 16_000, largestLazyChunk: 90_000 };
const root = process.argv[2] ?? '../server/web';
const gz = (file) => gzipSync(readFileSync(join(root, file)), { level: 9 }).length;
const html = readFileSync(join(root, 'index.html'), 'utf8');
const referenced = [...html.matchAll(/(?:src|href)="\.\/(assets\/[^"]+)"/g)].map((m) => m[1]);
const initialJs = referenced.filter((f) => f.endsWith('.js')).reduce((sum, f) => sum + gz(f), 0);
const initialCss = referenced.filter((f) => f.endsWith('.css')).reduce((sum, f) => sum + gz(f), 0);
const lazy = readdirSync(join(root, 'assets'))
  .map((f) => 'assets/' + f)
  .filter((f) => f.endsWith('.js') && !referenced.includes(f))
  .map((f) => [f, gz(f)])
  .sort((a, b) => b[1] - a[1]);
const kb = (n) => (n / 1000).toFixed(1) + ' kB';
console.log(`initial JS  ${kb(initialJs)} / ${kb(BUDGET.initialJs)}`);
console.log(`initial CSS ${kb(initialCss)} / ${kb(BUDGET.initialCss)}`);
for (const [file, size] of lazy.slice(0, 6)) console.log(`lazy        ${kb(size)}  ${file}`);
const problems = [];
if (initialJs > BUDGET.initialJs) problems.push('initial JS over budget');
if (initialCss > BUDGET.initialCss) problems.push('initial CSS over budget');
if (lazy.length && lazy[0][1] > BUDGET.largestLazyChunk) problems.push(`lazy chunk ${lazy[0][0]} over budget`);
if (problems.length) {
  console.error('FAILED: ' + problems.join('; '));
  process.exit(1);
}
