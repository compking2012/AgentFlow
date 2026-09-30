import {openDatabase} from './storage.mjs';

const error = (message, status = 400) => Object.assign(new Error(message), {status});
export function validateBook(input, previous = {title: '', author: '', read: false}) {
  const title = input.title === undefined ? previous.title : input.title;
  const author = input.author === undefined ? previous.author : input.author;
  const read = input.read === undefined ? previous.read : input.read;
  if (typeof title !== 'string' || !title.trim()) throw error('title_required');
  if ([...title.trim()].length > 120) throw error('title_too_long');
  if (typeof author !== 'string' || [...author].length > 120) throw error('invalid_author');
  if (typeof read !== 'boolean') throw error('invalid_read_state');
  return {title: title.trim(), author: author.trim(), read};
}

const wire = row => row ? {id: row.id, title: row.title, author: row.author, read: Boolean(row.is_read)} : null;
export class BookStore {
  constructor(directory) {
    this.db = openDatabase(directory);
    this.db.exec('CREATE TABLE IF NOT EXISTS books(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, author TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0 CHECK(is_read IN(0,1)))');
  }
  list(filter = 'all') {
    if (!['all', 'read', 'unread'].includes(filter)) throw error('invalid_filter');
    const sql = 'SELECT * FROM books' + (filter === 'all' ? '' : ' WHERE is_read = ?') + ' ORDER BY id DESC';
    return (filter === 'all' ? this.db.prepare(sql).all() : this.db.prepare(sql).all(filter === 'read' ? 1 : 0)).map(wire);
  }
  get(id) { return wire(this.db.prepare('SELECT * FROM books WHERE id = ?').get(id)); }
  create(input) {
    const value = validateBook(input);
    const result = this.db.prepare('INSERT INTO books(title,author,is_read) VALUES(?,?,?)').run(value.title, value.author, Number(value.read));
    return this.get(Number(result.lastInsertRowid));
  }
  update(id, input) {
    const previous = this.get(id);
    if (!previous) throw error('not_found', 404);
    const value = validateBook(input, previous);
    this.db.prepare('UPDATE books SET title=?,author=?,is_read=? WHERE id=?').run(value.title, value.author, Number(value.read), id);
    return this.get(id);
  }
  remove(id) {
    if (!this.get(id)) throw error('not_found', 404);
    this.db.prepare('DELETE FROM books WHERE id=?').run(id);
  }
  close() { this.db.close(); }
}
