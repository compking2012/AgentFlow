'use strict';
const {parse, parseExpression} = require('@babel/parser');
const {createHash} = require('node:crypto');
const fs = require('node:fs');
const options = {sourceType:'unambiguous', plugins:['typescript','jsx'], errorRecovery:false};
const digest = x => createHash('sha256').update(x).digest('hex').slice(0,24);
function children(node) {
  return Object.entries(node).filter(([k]) => !['loc','extra','comments','tokens','leadingComments','trailingComments','innerComments'].includes(k))
    .flatMap(([,v]) => Array.isArray(v) ? v : [v]).filter(v => v && typeof v.type === 'string');
}
function chain(node) {
  if (node?.type === 'Identifier') return [node.name];
  if (node?.type === 'MemberExpression' && !node.computed && node.property.type === 'Identifier') {
    const base = chain(node.object); return base && [...base,node.property.name];
  }
  return null;
}
function literal(node) {
  if (['StringLiteral','NumericLiteral','BooleanLiteral','NullLiteral'].includes(node?.type)) return true;
  if (node?.type === 'UnaryExpression' && ['-','+'].includes(node.operator) && node.argument.type === 'NumericLiteral') return true;
  if (node?.type === 'ArrayExpression') return node.elements.every(x => x && literal(x));
  if (node?.type === 'ObjectExpression') {
    const keys = new Set();
    return node.properties.every(p => {
      if (p.type !== 'ObjectProperty' || p.computed || p.shorthand || !['Identifier','StringLiteral','NumericLiteral'].includes(p.key.type)) return false;
      const key = p.key.name ?? String(p.key.value);
      if (keys.has(key) || key === '__proto__') return false;
      keys.add(key); return literal(p.value);
    });
  }
  return false;
}
function value(node) {
  if(node.type === 'NullLiteral') return null;
  if(node.type === 'UnaryExpression') return node.operator === '-' ? -node.argument.value : node.argument.value;
  if(node.type === 'ArrayExpression') return node.elements.map(value);
  if(node.type === 'ObjectExpression') return Object.fromEntries(node.properties.map(p => [p.key.name ?? String(p.key.value),value(p.value)]));
  return node.value;
}
function inspect(file, source) {
  const ast = parse(source, options), cases = [], counts = new Map();
  const slice = n => source.slice(n.start,n.end);
  const range = n => [Array.from(source.slice(0,n.start)).length,Array.from(source.slice(0,n.end)).length];
  const imports = new Map(), parents = new WeakMap();
  for(const statement of ast.program.body) {
    if(statement.type !== 'ImportDeclaration' || !['node:assert','node:assert/strict','assert','assert/strict'].includes(statement.source.value)) continue;
    const strict = statement.source.value.endsWith('/strict');
    for(const specifier of statement.specifiers) {
      const imported = specifier.type === 'ImportSpecifier' ? specifier.imported.name : null;
      if(imported === null || imported === 'strict') imports.set(specifier.local.name,{namespace:true,strict:strict || imported === 'strict'});
      else if(['strictEqual','deepStrictEqual'].includes(imported) || strict && ['equal','deepEqual'].includes(imported))
        imports.set(specifier.local.name,{namespace:false,method:imported,strict});
    }
  }
  function index(node) { for(const child of children(node)) { parents.set(child,node); index(child); } }
  index(ast);
  const unsafe = new Set();
  function references(node) {
    if(node.type === 'ImportDeclaration') return;
    if(node.type === 'Identifier' && imports.has(node.name)) {
      const parent = parents.get(node);
      if(parent?.type === 'MemberExpression' && !parent.computed && parent.property === node) return;
      if(parent?.type === 'ObjectProperty' && !parent.computed && !parent.shorthand && parent.key === node) return;
      let call = node, ancestor = parent;
      while(ancestor?.type === 'MemberExpression' && ancestor.object === call) {
        call = ancestor; ancestor = parents.get(call);
      }
      if(ancestor?.type !== 'CallExpression' || ancestor.callee !== call) unsafe.add(node.name);
    }
    for(const child of children(node)) references(child);
  }
  references(ast);
  function nativeMatcher(c) {
    if(!c || unsafe.has(c[0])) return null;
    const binding = imports.get(c[0]); if(!binding) return null;
    let method, strict = binding.strict;
    if(!binding.namespace && c.length === 1) method = binding.method;
    else if(binding.namespace && c.length === 2) method = c[1];
    else if(binding.namespace && c.length === 3 && c[1] === 'strict') { method = c[2]; strict = true; }
    if(method === 'strictEqual' || strict && method === 'equal') return 'node:assert.strictEqual';
    if(method === 'deepStrictEqual' || strict && method === 'deepEqual') return 'node:assert.deepStrictEqual';
    return null;
  }
  function assertion(current,node,matcher,actual,expected) {
    current.assertions.push({assertion_id:current.case_id+':assertion:'+current.assertions.length,
      matcher, actual_expression:slice(actual), expected_source:expected ? slice(expected) : null,
      old_expected:expected ? slice(expected) : null, expected_range:expected ? range(expected) : null,
      expected_value:expected && literal(expected) ? value(expected) : null,
      static_expected:!!expected && literal(expected), argument_count:node.arguments.length, range:range(node)});
  }
  function walk(node, suites=[], current=null) {
    if(node.type === 'CallExpression') {
      const c = chain(node.callee);
      const native = current && nativeMatcher(c);
      if(native && node.arguments.length >= 2 && !node.arguments.slice(0,2).some(a => a.type === 'SpreadElement'))
        assertion(current,node,native,node.arguments[0],node.arguments[1]);
      if(c && ['test','it','describe'].includes(c[0]) && !c.includes('step')) {
        const suite = c[0] === 'describe' || c.includes('describe');
        const name = node.arguments[0];
        const callback = node.arguments.find(x => ['ArrowFunctionExpression','FunctionExpression'].includes(x.type));
        if(name?.type === 'StringLiteral' && callback) {
          if(suite) { walk(callback,[...suites,name.value],current); return; }
          const full = [...suites,name.value].join(' > ');
          const count = counts.get(full) || 0; counts.set(full,count+1);
          const item = {case_id:'case_'+digest(file+'\0'+full+'\0'+count), name:full, title_path:[...suites,name.value],
            framework_operation:c.join('.'), range:range(node), assertions:[]};
          cases.push(item); walk(callback,suites,item); return;
        }
      }
      if(current && node.callee.type === 'MemberExpression' && !node.callee.computed) {
        let receiver = node.callee.object, modifiers = [];
        while(receiver?.type === 'MemberExpression' && !receiver.computed) {
          modifiers.unshift(receiver.property.name); receiver = receiver.object;
        }
        if(receiver?.type === 'CallExpression' && receiver.callee.type === 'Identifier' && receiver.callee.name === 'expect' && receiver.arguments.length >= 1) {
          const expected = node.arguments[0];
          current.assertions.push({assertion_id:current.case_id+':assertion:'+current.assertions.length,
            matcher:[...modifiers,node.callee.property.name].join('.'), actual_expression:slice(receiver.arguments[0]),
            expected_source:expected ? slice(expected) : null, old_expected:expected ? slice(expected) : null,
            expected_range:expected ? range(expected) : null, expected_value:expected && literal(expected) ? value(expected) : null,
            static_expected:!!expected && literal(expected), argument_count:node.arguments.length, range:range(node)});
        }
      }
    }
    for(const child of children(node)) walk(child,suites,current);
  }
  walk(ast);
  const dependencies = ast.program.body.filter(n => ['ImportDeclaration','ExportNamedDeclaration','ExportAllDeclaration'].includes(n.type) && n.source?.type === 'StringLiteral').map(n => n.source.value);
  return {file,path:file,parser:'@babel/parser',cases,imports:dependencies,source_digest:createHash('sha256').update(source).digest('hex')};
}
try {
  const input = JSON.parse(fs.readFileSync(0,'utf8'));
  if(input.operation === 'literal') {
    const node = parseExpression(input.source,options);
    // parseExpression rejects trailing tokens; source ranges also reject leading/trailing comments.
    const safe = literal(node) && node.start === 0 && node.end === input.source.length;
    process.stdout.write(JSON.stringify({safe,value:safe ? value(node) : null}));
  } else if(input.operation === 'coverage') {
    process.stdout.write(JSON.stringify(require('./coverage.cjs')(input, inspect)));
  } else process.stdout.write(JSON.stringify(inspect(input.file,input.source)));
} catch(error) { process.stdout.write(JSON.stringify({error:String(error.message)})); process.exitCode=1; }
