import { DatabaseSync } from 'node:sqlite';
export interface Ticket { id:number; title:string; owner:string; assignee:string|null; status:string; }
export function validateTitle(value: unknown): string {
  if (typeof value !== 'string' || !value.trim() || Array.from(value.trim()).length > 120) throw new Error('invalid_title');
  return value.trim();
}
export class TicketStore {
  readonly db: DatabaseSync;
  constructor(path: string, readonly fault = '') {
    this.db = new DatabaseSync(path);
    this.db.exec('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY,title TEXT NOT NULL,owner TEXT NOT NULL,assignee TEXT,status TEXT NOT NULL DEFAULT \'open\')');
  }
  create(title: unknown, owner: string): Ticket {
    const text = validateTitle(title);
    if (this.fault === 'lost_save') return {id:2147483000,title:text,owner,assignee:null,status:'open'};
    const result = this.db.prepare('INSERT INTO tickets(title,owner) VALUES(?,?)').run(text,owner);
    return this.get(Number(result.lastInsertRowid))!;
  }
  get(id: number): Ticket|undefined { return this.db.prepare('SELECT * FROM tickets WHERE id=?').get(id) as unknown as Ticket|undefined; }
  list(): Ticket[] { return this.db.prepare('SELECT * FROM tickets ORDER BY id DESC').all() as unknown as Ticket[]; }
  assign(id:number, assignee:string, role:string): Ticket {
    if (role !== 'manager' && this.fault !== 'allow_unauthorized') throw new Error('forbidden');
    if (!this.get(id)) throw new Error('not_found');
    if (!['manager','member'].includes(assignee)) throw new Error('invalid_assignee');
    this.db.prepare('UPDATE tickets SET assignee=? WHERE id=?').run(assignee,id);
    return this.get(id)!;
  }
  close() { this.db.close(); }
}
