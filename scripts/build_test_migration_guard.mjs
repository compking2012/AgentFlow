// Run with Node from the repository root; runtime bundle never imports dashboard dependencies.
import {createRequire} from 'node:module';
import {copyFileSync, readFileSync, writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
const require = createRequire(root+'apps/dashboard/package.json');
const {buildSync} = require('esbuild');
const resource = root+'src/agentflow/resources/test_migration_guard/';
buildSync({entryPoints:[resource+'inspect.cjs'],outfile:resource+'parser.bundle.cjs',bundle:true,platform:'node',target:'node18',nodePaths:[root+'apps/dashboard/node_modules'],minify:true,legalComments:'eof'});
copyFileSync(require.resolve('@babel/parser/package.json').replace(/package.json$/,'LICENSE'), resource+'BABEL-LICENSE');
const pkg=JSON.parse(readFileSync(require.resolve('@babel/parser/package.json'),'utf8'));
writeFileSync(resource+'PROVENANCE.json',JSON.stringify({parser:pkg.name,version:pkg.version,license:pkg.license,build:'node scripts/build_test_migration_guard.mjs'},null,2)+'\n');
