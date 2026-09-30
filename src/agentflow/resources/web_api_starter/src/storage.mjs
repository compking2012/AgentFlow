import {mkdirSync} from 'node:fs';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';

// Infrastructure only: the implementation Agent must define the product schema.
export function openDatabase(directory = process.env.AGENTFLOW_DATA_DIR || path.resolve('.agentflow-product-data')) {
  const root = path.resolve(directory);
  mkdirSync(root, {recursive: true, mode: 0o700});
  const database = new DatabaseSync(path.join(root, 'product.sqlite'));
  database.exec('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;');
  return database;
}
