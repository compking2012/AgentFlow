'use strict';
const {parse} = require('@babel/parser');
const options = {sourceType: 'unambiguous', plugins: ['typescript', 'jsx'], errorRecovery: false};
const metadata = new Set(['start', 'end', 'loc', 'extra', 'comments', 'tokens',
  'leadingComments', 'trailingComments', 'innerComments', 'errors']);
const functions = new Set(['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression',
  'ObjectMethod', 'ClassMethod', 'ClassPrivateMethod']);
const forbiddenGlobals = new Set(['process', 'global', 'globalThis', 'window', 'self', 'Deno', 'Bun',
  'eval', 'Function', 'require', 'module', 'exports', 'Reflect', 'WebAssembly', 'jest', 'vi']);
const forbiddenMembers = new Set(['skip', 'only', 'todo', 'configure', 'setTimeout', 'setDefaultTimeout',
  'setDefaultNavigationTimeout', '__proto__', 'prototype', 'constructor',
  '__defineGetter__', '__defineSetter__', '__lookupGetter__', '__lookupSetter__']);
const forbiddenModules = /^(?:node:)?(?:process|child_process|cluster|worker_threads|vm|module|inspector|fs)(?:\/|$)/;

function entries(node) { return Object.entries(node).filter(([key]) => !metadata.has(key)); }
function children(node) {
  return entries(node).flatMap(([, value]) => Array.isArray(value) ? value : [value])
    .filter(value => value && typeof value.type === 'string');
}
function normalized(node) {
  if (Array.isArray(node)) return node.map(normalized);
  if (!node || typeof node !== 'object') return node;
  return Object.fromEntries(entries(node).map(([key, value]) => [key, normalized(value)]));
}
const equal = (left, right) => JSON.stringify(normalized(left)) === JSON.stringify(normalized(right));
function chain(node) {
  if (node?.type === 'Identifier') return [node.name];
  if (['MemberExpression', 'OptionalMemberExpression'].includes(node?.type)) {
    const property = !node.computed && node.property.type === 'Identifier' ? node.property.name
      : node.computed && node.property.type === 'StringLiteral' ? node.property.value : null;
    const base = chain(node.object);
    return base && property !== null ? [...base, property] : null;
  }
  return null;
}
function patternNames(node, names = new Set()) {
  if (!node) return names;
  if (node.type === 'Identifier') names.add(node.name);
  else if (node.type === 'RestElement') patternNames(node.argument, names);
  else if (node.type === 'AssignmentPattern') patternNames(node.left, names);
  else if (node.type === 'ArrayPattern') node.elements.forEach(n => patternNames(n, names));
  else if (node.type === 'ObjectPattern') node.properties.forEach(n => patternNames(n.value || n.argument, names));
  return names;
}
function declarationNames(node) {
  const result = new Set();
  if (node.type === 'VariableDeclaration') node.declarations.forEach(d => patternNames(d.id, result));
  if (functions.has(node.type) || node.type === 'ClassDeclaration') patternNames(node.id, result);
  if (node.type === 'ImportDeclaration') node.specifiers.forEach(s => result.add(s.local.name));
  return result;
}
function testCallback(node) {
  if (node.type !== 'CallExpression') return null;
  const parts = chain(node.callee);
  if (!parts || !['test', 'it'].includes(parts[0]) || parts.includes('describe') || parts.includes('step')) return null;
  if (node.arguments[0]?.type !== 'StringLiteral') return null;
  return node.arguments.find(arg => ['ArrowFunctionExpression', 'FunctionExpression'].includes(arg.type)) || null;
}
function skeleton(node) {
  if (Array.isArray(node)) return node.map(skeleton);
  if (!node || typeof node !== 'object') return node;
  const callback = node.type && testCallback(node);
  return Object.fromEntries(entries(node).map(([key, value]) => [key,
    callback && key === 'arguments' ? value.map(arg => arg === callback
      ? {...normalized(arg), body: '<existing test callback>'} : skeleton(arg)) : skeleton(value)]));
}
function error(message, node) {
  throw new Error(`${message}${node?.loc ? ` (line ${node.loc.start.line})` : ''}`);
}
function identifierReference(node, parent) {
  if (node.type !== 'Identifier') return false;
  if (['MemberExpression', 'OptionalMemberExpression'].includes(parent?.type)
      && parent.property === node && !parent.computed) return false;
  if (['ObjectProperty', 'ObjectMethod'].includes(parent?.type) && parent.key === node
      && !parent.computed && !parent.shorthand) return false;
  return true;
}

module.exports = function coverage(input, inspect) {
  try {
    const before = parse(input.before, options), after = parse(input.after, options);
    const manifest = inspect(input.file, input.before), updated = inspect(input.file, input.after);
    if (!manifest.cases.length) error('Authorized file must contain existing statically named test/it cases');
    const inventory = m => m.cases.map(c => [c.case_id, c.name, c.framework_operation, c.title_path]);
    if (JSON.stringify(inventory(manifest)) !== JSON.stringify(inventory(updated)))
      error('Original test inventory, names and framework operations must remain unchanged');

    // Include references as well as declarations: adding a hoisted binding must
    // not capture a previously unresolved reference used by an original check.
    const protectedNames = new Set();
    function protect(node, parent) {
      if (identifierReference(node, parent)) protectedNames.add(node.name);
      for (const child of children(node)) protect(child, node);
    }
    protect(before, null);
    const authorities = new Set(['test', 'it', 'describe', 'expect', 'assert', 'Object']);
    const registration = new Set(['test', 'it', 'describe', 'before', 'after', 'beforeEach', 'afterEach',
      'beforeAll', 'afterAll', 'suite']);
    const hazards = new Set(forbiddenGlobals);
    for (const node of after.program.body) {
      if (node.type !== 'ImportDeclaration') continue;
      if (/^(?:node:)?assert(?:\/strict)?$/.test(node.source.value)
          || ['node:test', '@playwright/test', 'vitest', 'jest'].includes(node.source.value))
        node.specifiers.forEach(specifier => authorities.add(specifier.local.name));
      if (forbiddenModules.test(node.source.value))
        node.specifiers.forEach(specifier => hazards.add(specifier.local.name));
      if (['node:test', '@playwright/test', 'vitest', 'jest'].includes(node.source.value))
        node.specifiers.filter(specifier => specifier.imported?.name !== 'expect')
          .forEach(specifier => registration.add(specifier.local.name));
    }
    // Old aliases remain untouched, but they cannot provide a new route to
    // registration, assertion replacement or process control in appended code.
    function aliases(node) {
      if (node.type === 'VariableDeclarator') {
        const source = chain(node.init)?.[0];
        if (source) for (const name of patternNames(node.id)) {
          if (authorities.has(source)) authorities.add(name);
          if (registration.has(source)) registration.add(name);
          if (hazards.has(source) || source === 'Object') hazards.add(name);
        }
      }
      for (const child of children(node)) aliases(child);
    }
    let known;
    do {
      known = authorities.size + registration.size + hazards.size;
      aliases(before);
    } while (known !== authorities.size + registration.size + hazards.size);
    const parents = new WeakMap();
    function index(node) {
      for (const child of children(node)) { parents.set(child, node); index(child); }
    }
    index(after);
    const topLevelNames = new Set(protectedNames);
    const additions = [];
    let addedTopLevelNodes = 0;

    function safeAdded(node, locals = new Set(), helper = false, parent = null) {
      if (!node || typeof node.type !== 'string') return;
      if (identifierReference(node, parent) && hazards.has(node.name))
        error(`Added code cannot access hazardous global ${node.name}`, node);
      if (identifierReference(node, parent) && authorities.has(node.name) && !locals.has(node.name)) {
        let access = node, ancestor = parents.get(access);
        while (['MemberExpression', 'OptionalMemberExpression'].includes(ancestor?.type) && ancestor.object === access) {
          access = ancestor;
          ancestor = parents.get(access);
        }
        if (!chain(access) || !['CallExpression', 'OptionalCallExpression'].includes(ancestor?.type)
            || ancestor.callee !== access)
          error('Assertion/framework bindings may only be called directly; aliases and escapes are forbidden', node);
      }
      if (['ImportExpression', 'Import', 'WithStatement', 'DebuggerStatement'].includes(node.type))
        error(`Added ${node.type} is not supported`, node);
      if (['ReturnStatement', 'BreakStatement', 'ContinueStatement', 'ThrowStatement'].includes(node.type) && !helper)
        error('Added control exits can skip original checks; append assertions without exiting the callback', node);
      if (['MemberExpression', 'OptionalMemberExpression'].includes(node.type)) {
        if (node.computed && !['StringLiteral', 'NumericLiteral'].includes(node.property.type))
          error('Dynamic computed access is unsupported in additions; use a static property or index', node);
        const parts = chain(node), member = !node.computed ? node.property.name : node.property.value;
        if (forbiddenMembers.has(member)) error(`Added access to ${member} can alter tests or shared prototypes`, node);
        if (parts && (registration.has(parts[0]) || parts[0] === 'expect')
            && (parts[0] !== 'expect' || parts.length > 1 && !['soft', 'poll'].includes(parts[1])))
          error('Added framework configuration or test registration is forbidden', node);
      }
      if (['ObjectProperty', 'ObjectMethod', 'ClassMethod', 'ClassProperty'].includes(node.type)) {
        if (node.computed && !['StringLiteral', 'NumericLiteral'].includes(node.key.type))
          error('Dynamic computed declaration/destructuring keys are unsupported in additions', node);
        const key = node.key.name ?? node.key.value;
        if (forbiddenMembers.has(key)) error(`Added declaration/destructuring access to ${key} is forbidden`, node);
      }
      if (['CallExpression', 'OptionalCallExpression', 'NewExpression'].includes(node.type)) {
        const parts = chain(node.callee);
        if (parts && registration.has(parts[0]))
          error('Adding tests or changing test configuration is forbidden', node);
        if (parts?.[0] === 'Object' && parts.length > 1
            && !['keys', 'values', 'entries', 'is', 'hasOwn', 'getOwnPropertyNames'].includes(parts[1]))
          error('Added Object mutation/reflection can alter protected bindings', node);
      }
      if (['AssignmentExpression', 'UpdateExpression'].includes(node.type)
          || node.type === 'UnaryExpression' && node.operator === 'delete') {
        const target = node.left || node.argument;
        if (target.type !== 'Identifier')
          error('Added member/destructuring writes can mutate aliased original objects; assign a fresh identifier instead', node);
        const names = target.type === 'Identifier' ? new Set([target.name]) : patternNames(target);
        let root = target;
        while (['MemberExpression', 'OptionalMemberExpression'].includes(root.type)) root = root.object;
        if (root.type === 'Identifier') names.add(root.name);
        if (!names.size || [...names].some(name => !locals.has(name)))
          error('Added writes/deletes may not mutate or rebind original or ambient bindings', node);
      }
      for (const name of declarationNames(node)) {
        if (!helper && protectedNames.has(name)) error(`Added declaration shadows protected identifier ${name}`, node);
        locals.add(name);
      }
      // Fresh function scopes cannot shadow any original code. Their own local
      // parameters and variables may use ordinary names and normal return/finally.
      if (functions.has(node.type)) {
        const nested = new Set(locals);
        patternNames(node.id, nested);
        node.params.forEach(param => patternNames(param, nested));
        function collect(current) {
          for (const child of children(current)) {
            for (const name of declarationNames(child)) nested.add(name);
            if (!functions.has(child.type)) collect(child);
          }
        }
        collect(node.body);
        for (const param of node.params) safeAdded(param, nested, true, node);
        safeAdded(node.body, nested, true, node);
        return;
      }
      if (node.type === 'VariableDeclarator' && node.init) {
        const alias = chain(node.init);
        if (alias && protectedNames.has(alias[0]) && !locals.has(alias[0]))
          error('Aliasing an original binding in added code is not supported; call it directly', node);
      }
      for (const child of children(node)) safeAdded(child, locals, helper, node);
    }

    function inert(node) {
      if (!node) return false;
      if (['StringLiteral', 'NumericLiteral', 'BooleanLiteral', 'NullLiteral', 'BigIntLiteral', 'RegExpLiteral',
           'FunctionExpression', 'ArrowFunctionExpression'].includes(node.type)) return true;
      if (node.type === 'UnaryExpression' && ['+', '-', '!', '~', 'void'].includes(node.operator)) return inert(node.argument);
      if (node.type === 'TemplateLiteral') return node.expressions.every(inert);
      if (node.type === 'ArrayExpression') return node.elements.every(n => n && inert(n));
      if (node.type === 'ObjectExpression') return node.properties.every(p => p.type === 'ObjectProperty'
        && !p.computed && !p.shorthand && !['__proto__', 'prototype', 'constructor'].includes(p.key.name || p.key.value)
        && inert(p.value));
      return false;
    }
    function topLevel(node) {
      if (node.type === 'ImportDeclaration') {
        if (forbiddenModules.test(node.source.value) || ['node:test', '@playwright/test', 'vitest', 'jest'].includes(node.source.value))
          error('New imports of process, mutation or test-configuration APIs are forbidden', node);
      } else if (node.type === 'FunctionDeclaration') {
        if (!node.id) error('New helper functions must have fresh names', node);
      } else if (node.type !== 'VariableDeclaration' || node.kind !== 'const'
          || !node.declarations.every(d => d.id.type === 'Identifier' && inert(d.init))) {
        error('New top-level code must be a static import or inert fresh function/constant declaration', node);
      }
      const names = declarationNames(node);
      for (const name of names) {
        if (topLevelNames.has(name) || forbiddenGlobals.has(name))
          error(`New top-level binding must be fresh: ${name}`, node);
        topLevelNames.add(name);
      }
      if (node.type !== 'ImportDeclaration') safeAdded(node, new Set(names), false);
      addedTopLevelNodes++;
    }
    function extendsNode(old, next, inTest = false) {
      if (equal(old, next)) return;
      if (!old || !next || typeof old !== 'object' || typeof next !== 'object')
        error('An original test statement, parameter or operation was changed', next);
      if (Array.isArray(old)) {
        if (!Array.isArray(next) || old.length !== next.length) error('An original statement/argument list changed');
        old.forEach((node, i) => extendsNode(node, next[i], inTest));
        return;
      }
      if (old.type !== next.type) error('An original statement was replaced or moved', next);
      const oldKeys = entries(old).map(([key]) => key), nextKeys = entries(next).map(([key]) => key);
      if (JSON.stringify(oldKeys) !== JSON.stringify(nextKeys)) error('An original AST structure changed', next);
      const callback = testCallback(old);
      for (const key of oldKeys) {
        if (inTest && old.type === 'BlockStatement' && key === 'body') {
          if (next.body.length < old.body.length) error('Original block statements cannot be deleted', next);
          old.body.forEach((statement, i) => extendsNode(statement, next.body[i], true));
          additions.push(...next.body.slice(old.body.length));
        } else if (callback && key === 'arguments') {
          if (old.arguments.length !== next.arguments.length) error('Original test arguments must remain unchanged', next);
          old.arguments.forEach((arg, i) => extendsNode(arg, next.arguments[i], inTest || arg === callback));
        } else extendsNode(old[key], next[key], inTest);
      }
    }
    const oldProgram = before.program, nextProgram = after.program;
    // Program directives/interpreter/source type are executable semantics too.
    if (!equal({...oldProgram, body: []}, {...nextProgram, body: []}))
      error('Original program directives, interpreter and source type must remain unchanged');
    let i = 0;
    for (const node of nextProgram.body) {
      const old = oldProgram.body[i];
      if (old && JSON.stringify(skeleton(old)) === JSON.stringify(skeleton(node))) {
        extendsNode(old, node);
        i++;
      } else topLevel(node);
    }
    if (i !== oldProgram.body.length) error('Original top-level statements must remain unchanged and in order', oldProgram.body[i]);
    for (const node of additions) safeAdded(node);
    return {ok: true, errors: [], evidence: {file: input.file, parser: '@babel/parser',
      case_ids: manifest.cases.map(c => c.case_id),
      original_assertion_ids: manifest.cases.flatMap(c => c.assertions.map(a => a.assertion_id)),
      added_statements: additions.length, added_top_level_nodes: addedTopLevelNodes,
      before_source_digest: manifest.source_digest, after_source_digest: updated.source_digest,
      requires_independent_review: true}};
  } catch (caught) {
    return {ok: false, errors: [String(caught.message)]};
  }
};
