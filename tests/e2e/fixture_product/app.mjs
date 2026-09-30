const form = document.querySelector('#book-form');
const list = document.querySelector('#books');
const filter = document.querySelector('#filter');
const alert = document.querySelector('#error');
let editing = null;
async function request(path, method = 'GET', payload) {
  const response = await fetch(path, {method, headers: payload ? {'content-type': 'application/json'} : {}, body: payload ? JSON.stringify(payload) : undefined});
  if (!response.ok) { const body = await response.json(); throw new Error(body.error === 'title_required' ? '请输入书名' : '操作失败，请重试'); }
  return response.status === 204 ? null : response.json();
}
function failure(error) { alert.textContent = error.message; alert.hidden = false; }
function reset() { editing = null; form.reset(); document.querySelector('#save').textContent = '添加图书'; document.querySelector('#cancel').hidden = true; }
async function render() {
  const {books} = await request('/api/books?read=' + filter.value);
  list.replaceChildren(); document.querySelector('#empty').hidden = books.length > 0;
  for (const book of books) {
    const item = document.createElement('li'); item.dataset.bookId = book.id;
    const title = document.createElement('strong'); title.textContent = book.title;
    const author = document.createElement('span'); author.textContent = book.author || '作者未填写';
    const label = document.createElement('label');
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = book.read;
    checkbox.setAttribute('aria-label', '已读 ' + book.title); label.append(checkbox, '已读');
    checkbox.addEventListener('change', async () => { try { await request('/api/books/' + book.id, 'PATCH', {read: checkbox.checked}); await render(); } catch (error) { failure(error); } });
    const edit = document.createElement('button'); edit.textContent = '编辑'; edit.setAttribute('aria-label', '编辑 ' + book.title);
    edit.addEventListener('click', () => { editing = book.id; form.elements.title.value = book.title; form.elements.author.value = book.author; document.querySelector('#save').textContent = '保存修改'; document.querySelector('#cancel').hidden = false; form.elements.title.focus(); });
    const remove = document.createElement('button'); remove.textContent = '删除'; remove.setAttribute('aria-label', '删除 ' + book.title);
    remove.addEventListener('click', async () => { try { await request('/api/books/' + book.id, 'DELETE'); await render(); } catch (error) { failure(error); } });
    item.append(title, author, label, edit, remove); list.append(item);
  }
}
form.addEventListener('submit', async event => {
  event.preventDefault(); alert.hidden = true;
  try { await request(editing ? '/api/books/' + editing : '/api/books', editing ? 'PATCH' : 'POST', {title: form.elements.title.value, author: form.elements.author.value}); reset(); await render(); }
  catch (error) { failure(error); }
});
document.querySelector('#cancel').addEventListener('click', reset);
filter.addEventListener('change', () => render().catch(failure));
render().catch(failure);
