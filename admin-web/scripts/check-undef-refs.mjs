import fs from 'node:fs';
import path from 'node:path';
import { parse } from '@babel/parser';
import traverseModule from '@babel/traverse';

const traverse = traverseModule.default || traverseModule;
const SRC_DIR = path.resolve(process.cwd(), 'src');

const ALLOW_GLOBALS = new Set([
  'window', 'document', 'localStorage', 'sessionStorage', 'navigator', 'location', 'history',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame', 'cancelAnimationFrame',
  'URL', 'URLSearchParams', 'FormData', 'Blob', 'File', 'FileReader', 'Headers', 'Request', 'Response', 'fetch',
  'TextDecoder', 'CustomEvent',
  'console', 'Array', 'Object', 'Number', 'String', 'Boolean', 'Date', 'Math', 'JSON', 'Promise',
  'Set', 'Map', 'WeakSet', 'WeakMap', 'RegExp', 'Error', 'Intl', 'Symbol',
  'encodeURIComponent', 'decodeURIComponent', 'parseInt', 'parseFloat', 'isNaN', 'isFinite',
  'globalThis',
]);

function walkSourceFiles(dir, out = []) {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walkSourceFiles(full, out);
      continue;
    }
    if (/\.(jsx?|mjs)$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

function scanFile(filePath) {
  const code = fs.readFileSync(filePath, 'utf8');
  const ast = parse(code, {
    sourceType: 'module',
    plugins: ['jsx'],
  });

  const issues = [];
  const seen = new Set();
  traverse(ast, {
    ReferencedIdentifier(nodePath) {
      const name = nodePath.node.name;
      if (!name) return;
      if (ALLOW_GLOBALS.has(name)) return;
      if (nodePath.scope.hasBinding(name)) return;

      const line = nodePath.node.loc?.start?.line || 1;
      const column = nodePath.node.loc?.start?.column || 0;
      const dedupKey = `${name}:${line}:${column}`;
      if (seen.has(dedupKey)) return;
      seen.add(dedupKey);
      issues.push({ filePath, line, column, name });
    },
  });
  return issues;
}

if (!fs.existsSync(SRC_DIR)) {
  console.error(`[check:undef] src directory not found: ${SRC_DIR}`);
  process.exit(2);
}

const files = walkSourceFiles(SRC_DIR);
const findings = files.flatMap((f) => scanFile(f));

if (!findings.length) {
  console.log('[check:undef] OK: no undefined references found');
  process.exit(0);
}

console.error('[check:undef] Found undefined references:');
for (const item of findings) {
  const rel = path.relative(process.cwd(), item.filePath);
  console.error(`- ${rel}:${item.line}:${item.column} ${item.name}`);
}
process.exit(1);
