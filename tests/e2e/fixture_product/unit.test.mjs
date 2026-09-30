import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {BookStore, validateBook} from '../product/books.mjs';

test('book validation rejects blank titles', () => {
  assert.throws(() => validateBook({title: '   '}), /title_required/);
  assert.throws(() => validateBook({title: 'x'.repeat(121)}), /title_too_long/);
  assert.throws(() => validateBook({title: 'valid', read: 'yes'}), /invalid_read_state/);
  assert.equal(validateBook({title: ' valid ', author: ' author '}).title, 'valid');
});
test('book store supports CRUD and read filters', () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'reading-unit-')); const store = new BookStore(directory);
  try {
    const book = store.create({title: 'Example', author: 'Writer'});
    assert.equal(store.list('unread').length, 1);
    assert.equal(store.update(book.id, {title: 'Changed', read: true}).title, 'Changed');
    assert.equal(store.list('read')[0].id, book.id); assert.equal(store.list('unread').length, 0);
    store.remove(book.id); assert.equal(store.list().length, 0); assert.equal(store.get(book.id), null);
    assert.throws(() => store.update(book.id, {read: false}), /not_found/);
  } finally { store.close(); rmSync(directory, {recursive: true, force: true}); }
});
test('book state persists after reopening SQLite', () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'reading-persist-')); let store = new BookStore(directory);
  try {
    const book = store.create({title: 'Persistent', author: 'Reader', read: true}); store.close();
    store = new BookStore(directory); assert.deepEqual(store.get(book.id), book);
  } finally { store.close(); rmSync(directory, {recursive: true, force: true}); }
});
