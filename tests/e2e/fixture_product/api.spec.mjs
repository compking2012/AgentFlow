import {test, expect} from './support/fixtures.mjs';
test('API CRUD and read filters preserve exact business state', async ({request}) => {
  const created = await request.post('/api/books', {data: {title: 'API Book', author: 'Writer'}}); expect(created.status()).toBe(201);
  const book = await created.json(); expect(book.read).toBe(false);
  const changed = await request.patch('/api/books/' + book.id, {data: {title: 'API Changed', read: true}}); expect(changed.status()).toBe(200);
  const read = await request.get('/api/books/' + book.id); expect(await read.json()).toMatchObject({title: 'API Changed', author: 'Writer', read: true});
  const filtered = await request.get('/api/books?read=read'); expect((await filtered.json()).books.some(b => b.id === book.id)).toBe(true);
  const unread = await request.get('/api/books?read=unread'); expect((await unread.json()).books.some(b => b.id === book.id)).toBe(false);
  expect((await request.delete('/api/books/' + book.id)).status()).toBe(204);
  expect((await request.get('/api/books/' + book.id)).status()).toBe(404);
});
test('API invalid title does not create a book', async ({request}) => {
  const before = (await (await request.get('/api/books')).json()).books.length;
  expect((await request.post('/api/books', {data: {title: '  ', author: 'Invalid'}})).status()).toBe(400);
  expect((await (await request.get('/api/books')).json()).books.length).toBe(before);
});
test('API data remains available to a fresh request', async ({request}) => {
  const created = await request.post('/api/books', {data: {title: 'Reread Book', author: 'Reader'}}); const book = await created.json();
  expect((await (await request.get('/api/books/' + book.id)).json()).title).toBe('Reread Book');
});
