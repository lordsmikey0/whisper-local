const form = document.querySelector('#form');
const fileInput = document.querySelector('#file');
const controls = document.querySelector('#controls');
const zone = document.querySelector('#drop-zone');
const result = document.querySelector('#result');
const preview = document.querySelector('#audio-preview');
const selectedList = document.querySelector('#selected-files');
const queueList = document.querySelector('#queue-list');
const terminal = new Set(['done', 'error', 'cancelled']);
const localJobs = new Map();
const views = new Map();
let serverJobs = [];
let selected = [];
let uploading = false;
let viewingId = null;
let viewingSource = null;
let historyItems = [];
let historyFetchedAt = 0;
let completionSignature = '';
const historyViews = new Map();
const confirmingDelete = new Set();
const deletingHistory = new Set();

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function showStatus(message, kind = 'error') {
  const status = document.querySelector('#status');
  status.hidden = false;
  status.className = `status ${kind}`;
  document.querySelector('#status-text').textContent = message;
}
async function api(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Не удалось выполнить запрос.');
  return body;
}
function timeLabel(value) {
  if (value == null) return '';
  const seconds = Math.max(0, Math.floor(value));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
function renderSelected() {
  selectedList.replaceChildren();
  selectedList.hidden = !selected.length;
  selected.forEach((file, index) => {
    const row = element('li');
    row.append(element('span', '', file.name));
    const remove = element('button', 'remove-file', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', `Убрать ${file.name}`);
    remove.onclick = () => { selected.splice(index, 1); renderSelected(); };
    row.append(remove);
    selectedList.append(row);
  });
  const converting = form.elements.mode.value === 'convert';
  document.querySelector('#file-title').textContent = selected.length ? `Выбрано файлов: ${selected.length}`
    : converting ? 'Перетащите MP4 сюда' : 'Перетащите аудио или MP4 сюда';
  document.querySelector('#file-info').textContent = selected.length
    ? `${(selected.reduce((sum, file) => sum + file.size, 0) / 1024 / 1024).toLocaleString('ru-RU', {maximumFractionDigits: 1})} МБ · можно добавить ещё`
    : 'Можно сразу несколько · нажмите, чтобы выбрать';
  document.querySelector('#submit').disabled = uploading || !selected.length;
}
fileInput.addEventListener('change', () => {
  selected.push(...fileInput.files);
  fileInput.value = '';
  renderSelected();
});
for (const input of form.querySelectorAll('[name="mode"]')) input.addEventListener('change', () => {
  const converting = form.elements.mode.value === 'convert';
  document.querySelector('#settings').hidden = converting;
  document.querySelector('#offline-option').hidden = converting;
  document.querySelector('#convert-hint').hidden = !converting;
  fileInput.accept = converting ? 'video/mp4,.mp4' : 'audio/*,video/mp4,.mp3,.wav,.m4a,.flac,.ogg,.opus,.aac,.wma,.mp4,.webm';
  renderSelected();
});
for (const event of ['dragenter', 'dragover']) zone.addEventListener(event, e => {
  e.preventDefault();
  if (!uploading) zone.classList.add('dragover');
});
for (const event of ['dragleave', 'drop']) zone.addEventListener(event, () => zone.classList.remove('dragover'));
zone.addEventListener('drop', e => {
  e.preventDefault();
  if (uploading) return;
  selected.push(...e.dataTransfer.files);
  renderSelected();
});
window.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) e.preventDefault(); });
window.addEventListener('drop', e => { if (e.dataTransfer.types.includes('Files')) e.preventDefault(); });

function jobs() {
  const combined = new Map(serverJobs.map(job => [job.id, job]));
  for (const job of localJobs.values()) if (!combined.has(job.id)) combined.set(job.id, job);
  return [...combined.values()];
}
function renderQueue() {
  const all = jobs();
  const ids = new Set(all.map(job => job.id));
  for (const [id, view] of views) if (!ids.has(id)) { view.row.remove(); views.delete(id); }
  for (const [index, job] of all.entries()) {
    let view = views.get(job.id);
    if (!view) {
      const row = element('article', 'job');
      const top = element('div', 'job-top');
      const icon = element('span', 'file-icon', job.mode === 'convert' ? 'MP3' : 'TXT');
      const description = element('div', 'job-description');
      const name = element('h3', 'job-name', job.filename);
      const detail = element('p', 'job-detail');
      const actions = element('div', 'job-actions');
      const cancel = element('button', 'quiet-button', 'Отменить');
      cancel.type = 'button';
      cancel.setAttribute('aria-label', `Отменить ${job.filename}`);
      cancel.onclick = () => cancelJob(job.id).catch(error => showStatus(error.message));
      const open = element('button', 'quiet-button', 'Открыть');
      open.type = 'button';
      open.onclick = () => openResult(job).catch(error => showStatus(error.message));
      const download = element('a', 'download-small', 'Скачать ↓');
      download.href = `/api/jobs/${job.id}/download`;
      download.download = '';
      actions.append(cancel, open, download);
      description.append(name, detail);
      top.append(icon, description, actions);
      const bar = element('progress', 'job-progress');
      bar.max = 100;
      bar.setAttribute('aria-label', `Прогресс ${job.filename}`);
      const error = element('p', 'job-error');
      row.append(top, bar, error);
      view = {row, detail, cancel, open, download, bar, error};
      views.set(job.id, view);
      queueList.append(row);
    }
    if (queueList.children[index] !== view.row) queueList.insertBefore(view.row, queueList.children[index] || null);
    view.open.onclick = () => openResult(job).catch(error => showStatus(error.message));
    view.row.dataset.state = job.state;
    const active = !terminal.has(job.state);
    view.cancel.hidden = !active;
    view.cancel.disabled = job.state === 'cancelling';
    view.cancel.textContent = job.state === 'cancelling' ? 'Отменяю…' : 'Отменить';
    view.open.hidden = view.download.hidden = job.state !== 'done';
    view.bar.hidden = !active || job.state === 'queued';
    const progress = job.state === 'uploading' ? job.uploadProgress : job.progress;
    if (progress == null) view.bar.removeAttribute('value'); else view.bar.value = progress;
    const pieces = [job.stage];
    if (job.state === 'running' && progress != null) pieces.push(`${Math.floor(progress)}%`);
    if (job.state === 'running' && job.processed > 0) pieces.push(`${timeLabel(job.processed)}${job.total ? ` из ${timeLabel(job.total)}` : ' обработано'}`);
    if (job.device) pieces.push(job.device === 'cuda' ? 'GPU' : 'CPU');
    if (job.elapsed > 0) pieces.push(`${job.elapsed.toFixed(1)} сек.`);
    view.detail.textContent = pieces.join(' · ');
    view.error.hidden = !job.error;
    view.error.textContent = job.error || '';
  }
  document.querySelector('#queue-count').textContent = all.length;
  const done = all.filter(job => job.state === 'done').length;
  const waiting = all.filter(job => ['queued', 'uploading'].includes(job.state)).length;
  const failed = all.filter(job => job.state === 'error').length;
  const cancelled = all.filter(job => job.state === 'cancelled').length;
  const working = all.some(job => ['running', 'cancelling'].includes(job.state));
  document.querySelector('#queue-summary').textContent = !all.length ? 'Пока здесь пусто. Добавьте первую запись.'
    : [`Готово ${done} из ${all.length}`, working ? 'идёт обработка' : '', waiting ? `ожидают ${waiting}` : '', failed ? `ошибок ${failed}` : '', cancelled ? `отменено ${cancelled}` : ''].filter(Boolean).join(' · ');
  document.querySelector('#archive').hidden = !done;
  document.querySelector('#cancel-all').disabled = !all.some(job => !terminal.has(job.state));
}
async function cancelJob(id) {
  const local = localJobs.get(id);
  if (local) {
    local.state = 'cancelled';
    local.stage = 'Отменено';
    local.xhr?.abort();
    renderQueue();
  }
  await api(`/api/jobs/${id}/cancel`, {method: 'POST'});
  await refresh();
}
document.querySelector('#cancel-all').onclick = async () => {
  try {
    const cancellations = [...localJobs.values()].filter(job => !terminal.has(job.state)).map(job => cancelJob(job.id));
    await api('/api/jobs/cancel-all', {method: 'POST'});
    await Promise.all(cancellations);
    await refresh();
  } catch (error) { showStatus(error.message); }
};

function upload(job, file, options) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    job.xhr = xhr;
    job.state = 'uploading';
    job.stage = 'Передача файла';
    const data = new FormData();
    data.append('file', file);
    data.append('client_id', job.id);
    for (const [key, value] of Object.entries(options)) data.append(key, String(value));
    xhr.open('POST', '/api/jobs');
    xhr.upload.onprogress = e => {
      if (job.state === 'cancelled') return;
      job.uploadProgress = e.lengthComputable ? e.loaded / e.total * 100 : null;
      job.stage = job.uploadProgress >= 100 ? 'Добавляю в очередь…' : `Передача файла${job.uploadProgress == null ? '' : ` · ${Math.floor(job.uploadProgress)}%`}`;
      renderQueue();
    };
    xhr.onload = () => {
      try {
        const body = JSON.parse(xhr.responseText);
        if (xhr.status < 200 || xhr.status >= 300) throw new Error(typeof body.detail === 'string' ? body.detail : 'Не удалось добавить файл.');
        job.state = body.state;
        localJobs.delete(job.id);
        serverJobs = serverJobs.filter(item => item.id !== body.id);
        serverJobs.push(body);
        resolve();
      } catch (error) { reject(error); }
    };
    xhr.onerror = () => reject(new Error('Нет связи с сервером. Файл не удалось передать.'));
    xhr.onabort = () => reject(new Error('Передача отменена.'));
    xhr.send(data);
    renderQueue();
  });
}
form.addEventListener('submit', async e => {
  e.preventDefault();
  if (!selected.length || uploading) return;
  const mode = form.elements.mode.value;
  if (mode === 'convert' && selected.some(file => !/\.mp4$/i.test(file.name))) {
    showStatus('В режиме MP4 → MP3 выбирайте только видео MP4.');
    return;
  }
  const options = {mode, model: form.elements.model.value, language: form.elements.language.value,
    device: form.elements.device.value, offline: form.elements.offline.checked};
  const batch = selected.map(file => {
    const job = {id: crypto.randomUUID().replaceAll('-', ''), filename: file.name, mode, state: 'queued', stage: 'Ожидает передачи', elapsed: 0};
    localJobs.set(job.id, job);
    return {job, file};
  });
  selected = [];
  uploading = true;
  controls.disabled = true;
  renderSelected();
  renderQueue();
  showStatus('Передаю файлы на локальный сервер…', 'busy');
  try {
    for (const {job, file} of batch) {
      if (job.state === 'cancelled') continue;
      try { await upload(job, file, options); }
      catch (error) {
        if (job.state !== 'cancelled') { job.state = 'error'; job.stage = 'Ошибка передачи'; job.error = error.message; }
      }
      renderQueue();
    }
    const failed = batch.filter(({job}) => job.state === 'error').length;
    const cancelled = batch.filter(({job}) => job.state === 'cancelled').length;
    const accepted = batch.length - failed - cancelled;
    showStatus([`Добавлено в очередь: ${accepted}.`, failed ? `Ошибок передачи: ${failed}.` : '',
      cancelled ? `Отменено: ${cancelled}.` : '', 'Можно выбрать следующую партию.'].filter(Boolean).join(' '), failed ? 'error' : 'success');
  } finally {
    uploading = false;
    controls.disabled = false;
    renderSelected();
    await refresh();
  }
});
async function refresh() {
  try {
    const body = await api('/api/jobs');
    serverJobs = body.jobs;
    if (viewingId && viewingSource === 'queue' && !serverJobs.some(job => job.id === viewingId)) closeResult();
    document.querySelector('#connection').hidden = true;
    renderQueue();
    const signature = serverJobs.filter(job => job.state === 'done').map(job => job.id).join(',');
    if (signature !== completionSignature || Date.now() - historyFetchedAt > 5000) {
      completionSignature = signature;
      await refreshHistory();
    }
  } catch {
    document.querySelector('#connection').hidden = false;
  }
}
async function poll() {
  await refresh();
  setTimeout(poll, jobs().some(job => !terminal.has(job.state)) ? 650 : 2000);
}
function closeResult() {
  viewingId = null;
  viewingSource = null;
  preview.pause();
  preview.removeAttribute('src');
  preview.load();
  result.hidden = true;
}
document.querySelector('#close-result').onclick = closeResult;
async function openResult(job, source = 'queue') {
  closeResult();
  viewingId = job.id;
  viewingSource = source;
  const base = source === 'history' ? '/api/history' : '/api/jobs';
  const converting = job.mode === 'convert';
  const transcript = document.querySelector('#transcript');
  if (!converting) {
    const body = await api(`${base}/${job.id}/result`);
    if (viewingId !== job.id) return;
    transcript.value = body.text;
  } else {
    preview.src = `${base}/${job.id}/download`;
  }
  transcript.hidden = converting;
  preview.hidden = !converting;
  document.querySelector('#result-title').textContent = converting ? 'Ваша аудиодорожка' : 'Ваша стенограмма';
  document.querySelector('#result-info').textContent = job.output_name;
  const download = document.querySelector('#download');
  download.href = `${base}/${job.id}/download`;
  download.download = job.output_name;
  download.textContent = converting ? 'Скачать .mp3 ↓' : 'Скачать .txt ↓';
  result.hidden = false;
  result.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
renderSelected();
poll();

function sizeLabel(bytes) {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} ГБ`;
}
function historyMessage(message) {
  const status = document.querySelector('#history-status');
  status.textContent = message;
  status.hidden = !message;
}
function renderHistory() {
  const query = document.querySelector('#history-search').value.trim().toLocaleLowerCase('ru-RU');
  const mode = document.querySelector('#history-type').value;
  const filtered = historyItems.filter(item => (mode === 'all' || mode === item.mode)
    && `${item.filename} ${item.output_name}`.toLocaleLowerCase('ru-RU').includes(query));
  const ids = new Set(filtered.map(item => item.id));
  for (const [id, view] of historyViews) if (!ids.has(id)) { view.row.remove(); historyViews.delete(id); }
  const list = document.querySelector('#history-list');
  filtered.forEach((item, index) => {
    let view = historyViews.get(item.id);
    if (!view) {
      const row = element('article', 'job');
      const top = element('div', 'job-top');
      const icon = element('span', 'file-icon', item.mode === 'convert' ? 'MP3' : 'TXT');
      const description = element('div', 'job-description');
      const title = element('h3', 'job-name history-headline', item.output_name);
      const detail = element('p', 'job-detail');
      const actions = element('div', 'job-actions');
      const open = element('button', 'quiet-button', 'Открыть');
      open.type = 'button';
      const download = element('a', 'download-small', 'Скачать ↓');
      download.download = item.output_name;
      download.href = `/api/history/${item.id}/download`;
      const remove = element('button', 'quiet-button delete-button', 'Удалить');
      remove.type = 'button';
      remove.setAttribute('aria-label', `Удалить ${item.output_name} из истории`);
      remove.onclick = () => { confirmingDelete.add(item.id); renderHistory(); };
      const confirmation = element('div', 'delete-confirm');
      const confirmText = element('span', '', 'Удалить этот результат с диска?');
      const confirm = element('button', 'quiet-button delete-button', 'Да, удалить');
      confirm.type = 'button';
      confirm.onclick = () => deleteHistory(item.id);
      const keep = element('button', 'quiet-button', 'Оставить');
      keep.type = 'button';
      keep.onclick = () => { confirmingDelete.delete(item.id); renderHistory(); };
      confirmation.append(confirmText, confirm, keep);
      actions.append(open, download, remove);
      description.append(title, detail);
      top.append(icon, description, actions);
      row.append(top, confirmation);
      view = {row, detail, open, download, remove, confirmation, confirm, keep};
      historyViews.set(item.id, view);
      list.append(row);
    }
    if (list.children[index] !== view.row) list.insertBefore(view.row, list.children[index] || null);
    view.open.onclick = () => openResult(item, 'history').catch(error => historyMessage(error.message));
    view.open.disabled = !item.available;
    view.download.hidden = !item.available;
    view.row.classList.toggle('history-missing', !item.available);
    view.detail.textContent = [new Date(item.created_at).toLocaleString('ru-RU'), sizeLabel(item.size),
      item.available ? `Исходник: ${item.filename}` : 'Файл не найден на диске'].join(' · ');
    view.confirmation.hidden = !confirmingDelete.has(item.id);
    view.confirm.disabled = view.keep.disabled = view.remove.disabled = deletingHistory.has(item.id);
    view.confirm.textContent = deletingHistory.has(item.id) ? 'Удаляю…' : 'Да, удалить';
  });
  document.querySelector('#history-count').textContent = historyItems.length;
  document.querySelector('#history-summary').textContent = `${historyItems.length} сохранено · ${sizeLabel(historyItems.reduce((sum, item) => sum + item.size, 0))} · доступны после перезапуска`;
  const empty = document.querySelector('#history-empty');
  empty.hidden = !!filtered.length;
  empty.textContent = historyItems.length ? 'По вашему запросу ничего не найдено.'
    : 'История пока пуста. Добавьте файлы в очередь — готовые результаты сохранятся автоматически.';
}
async function refreshHistory() {
  try {
    const body = await api('/api/history');
    historyItems = body.items;
    historyFetchedAt = Date.now();
    if (viewingId && viewingSource === 'history' && !historyItems.some(item => item.id === viewingId)) closeResult();
    renderHistory();
    historyMessage('');
  } catch (error) { historyMessage(`Не удалось обновить историю: ${error.message}`); }
}
async function deleteHistory(id) {
  if (deletingHistory.has(id)) return;
  deletingHistory.add(id);
  if (viewingId === id) closeResult();
  renderHistory();
  try {
    await api(`/api/history/${id}`, {method: 'DELETE'});
    confirmingDelete.delete(id);
    await refreshHistory();
    await refresh();
  } catch (error) { historyMessage(error.message); }
  finally { deletingHistory.delete(id); renderHistory(); }
}
document.querySelector('#history-search').addEventListener('input', renderHistory);
document.querySelector('#history-type').addEventListener('change', renderHistory);
