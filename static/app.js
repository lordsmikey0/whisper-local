const form = document.querySelector('#form');
let listeningSegments = [];
const fileInput = document.querySelector('#file');
const controls = document.querySelector('#controls');
const zone = document.querySelector('#drop-zone');
const result = document.querySelector('#result');
const preview = document.querySelector('#audio-preview');
const videoPreview = document.querySelector('#video-preview');
const videoFormats = new Set(['mp4', 'mkv', 'mov', 'webm']);
function formatOf(item) { return item.output_format || item.options?.output_format || (item.mode === 'convert' ? 'mp3' : 'txt'); }
function kindOf(item) { return item.mode === 'transcribe' ? 'text' : videoFormats.has(formatOf(item)) ? 'video' : 'audio'; }
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
let speakerState = {ready:false, libraries_ready:false, model_ready:false, preparing:false};
let currentSpeakers = [];
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
    : 'Перетащите аудио или видео сюда';
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
  document.querySelector('#convert-settings').hidden = !converting;
  document.querySelector('#speaker-settings').hidden = converting;
  document.querySelector('#listening-option').hidden = converting;
  form.elements.keep_audio.disabled = converting;
  updateSpeakerControls();
  updateConversionSettings();
  renderSelected();
});
function updateConversionSettings() {
  const converting = form.elements.mode.value === 'convert';
  const format = form.elements.output_format.value;
  const video = videoFormats.has(format);
  const lossless = format === 'wav' || format === 'flac';
  for (const field of document.querySelectorAll('#convert-settings input, #convert-settings select')) field.disabled = !converting;
  for (const field of document.querySelectorAll('#video-options input, #video-options select')) field.disabled = !converting || !video;
  form.elements.quality.disabled = !converting || lossless;
  document.querySelector('#video-options').hidden = !video;
  document.querySelector('#quality-option').hidden = lossless;
  document.querySelector('#convert-hint').textContent = video
    ? (format === 'webm' ? 'Видео VP9. WebM может обрабатываться дольше других форматов.' : 'Видео H.264 с аудиодорожкой AAC. Совместимые параметры уже подобраны.')
    : lossless ? 'Формат без потерь: частота и каналы исходного аудио сохраняются.' : 'Аудио из записи или видео. Модель Whisper не нужна.';
}
form.elements.output_format.addEventListener('change', updateConversionSettings);
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
      const icon = element('span', 'file-icon', formatOf(job).toUpperCase());
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
  if (!form.checkValidity()) {
    const invalid = form.querySelector(':invalid');
    const details = invalid?.closest('details');
    if (details) details.open = true;
    form.reportValidity();
    return;
  }
  const mode = form.elements.mode.value;
  const options = {mode, model: form.elements.model.value, language: form.elements.language.value,
    device: form.elements.device.value, offline: form.elements.offline.checked};
  if (mode === 'transcribe') {
    options.diarize = form.elements.diarize.checked;
    options.keep_audio = form.elements.keep_audio.checked;
    if (options.diarize && form.elements.num_speakers.value !== '') options.num_speakers = Number(form.elements.num_speakers.value);
  }
  if (mode === 'convert') {
    options.output_format = form.elements.output_format.value;
    options.quality = form.elements.quality.value;
    options.trim_start = Number(form.elements.trim_start.value || 0);
    const end = form.elements.trim_end.value;
    if (end !== '') options.trim_end = Number(end);
    if (options.trim_end != null && options.trim_end <= options.trim_start) {
      document.querySelector('.advanced-settings').open = true;
      showStatus('Конец отрезка должен быть позже начала.'); return;
    }
    if (videoFormats.has(options.output_format)) {
      if (selected.some(file => /\.(mp3|wav|flac|m4a|aac|ogg|opus|wma)$/i.test(file.name))) {
        showStatus('Для сохранения видео нужны видеофайлы. Для аудиозаписей выберите аудиоформат.'); return;
      }
      options.resolution = form.elements.resolution.value;
      options.fps = form.elements.fps.value;
      options.mute = form.elements.mute.checked;
    }
  }
  const batch = selected.map(file => {
    const job = {id: crypto.randomUUID().replaceAll('-', ''), filename: file.name, mode, output_format: mode === 'convert' ? options.output_format : 'txt', state: 'queued', stage: 'Ожидает передачи', elapsed: 0};
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
  await refreshSpeakerStatus();
  setTimeout(poll, jobs().some(job => !terminal.has(job.state)) ? 650 : 2000);
}
function closeResult() {
  const audio = document.querySelector('#transcript-audio');
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  document.querySelector('#listening').hidden = true;
  document.querySelector('#listening-unavailable').hidden = true;
  document.querySelector('#transcript-segments').replaceChildren();
  listeningSegments = [];
  viewingId = null;
  viewingSource = null;
  preview.pause();
  preview.removeAttribute('src');
  preview.load();
  videoPreview.pause();
  videoPreview.removeAttribute('src');
  videoPreview.load();
  result.hidden = true;
  currentSpeakers = [];
  document.querySelector('#speaker-names').hidden = true;
}
document.querySelector('#close-result').onclick = closeResult;
async function openResult(job, source = 'queue') {
  closeResult();
  viewingId = job.id;
  viewingSource = source;
  const base = source === 'history' ? '/api/history' : '/api/jobs';
  const converting = job.mode === 'convert';
  const isVideo = kindOf(job) === 'video';
  const transcript = document.querySelector('#transcript');
  if (!converting) {
    const body = await api(`${base}/${job.id}/result`);
    if (viewingId !== job.id) return;
    transcript.value = body.text;
    renderSpeakerNames(body);
    renderListening(body, job.id);
  } else {
    (isVideo ? videoPreview : preview).src = `${base}/${job.id}/download`;
  }
  transcript.hidden = converting || !document.querySelector('#listening').hidden;
  preview.hidden = !converting || isVideo;
  videoPreview.hidden = !isVideo;
  document.querySelector('#playback-hint').hidden = !converting;
  document.querySelector('#result-title').textContent = converting ? (isVideo ? 'Ваше видео' : 'Ваше аудио') : 'Ваша стенограмма';
  document.querySelector('#result-info').textContent = job.output_name;
  const download = document.querySelector('#download');
  download.href = `${base}/${job.id}/download`;
  download.download = job.output_name;
  download.textContent = `Скачать .${formatOf(job)} ↓`;
  result.hidden = false;
  result.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
updateConversionSettings();
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
  const filtered = historyItems.filter(item => (mode === 'all' || mode === kindOf(item))
    && `${item.filename} ${item.output_name}`.toLocaleLowerCase('ru-RU').includes(query));
  const ids = new Set(filtered.map(item => item.id));
  for (const [id, view] of historyViews) if (!ids.has(id)) { view.row.remove(); historyViews.delete(id); }
  const list = document.querySelector('#history-list');
  filtered.forEach((item, index) => {
    let view = historyViews.get(item.id);
    if (!view) {
      const row = element('article', 'job');
      const top = element('div', 'job-top');
      const icon = element('span', 'file-icon', formatOf(item).toUpperCase());
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
    view.detail.textContent = [new Date(item.created_at).toLocaleString('ru-RU'), sizeLabel(item.size + (item.audio_size || 0)),
      item.available ? `Исходник: ${item.filename}` : 'Файл не найден на диске'].join(' · ');
    view.confirmation.hidden = !confirmingDelete.has(item.id);
    view.confirm.disabled = view.keep.disabled = view.remove.disabled = deletingHistory.has(item.id);
    view.confirm.textContent = deletingHistory.has(item.id) ? 'Удаляю…' : 'Да, удалить';
  });
  document.querySelector('#history-count').textContent = historyItems.length;
  document.querySelector('#history-summary').textContent = `${historyItems.length} сохранено · ${sizeLabel(historyItems.reduce((sum, item) => sum + item.size + (item.audio_size || 0), 0))} · доступны после перезапуска`;
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

function renderListening(body, id) {
  const panel = document.querySelector('#listening');
  const audio = document.querySelector('#transcript-audio');
  const list = document.querySelector('#transcript-segments');
  const enabled = !!body.audio_available;
  panel.hidden = !enabled;
  document.querySelector('#listening-unavailable').hidden = enabled;
  document.querySelector('#transcript').hidden = enabled;
  if (!enabled) return;
  // Keep playback position intact when speaker names are changed.
  if (audio.dataset.record !== id || !audio.getAttribute('src')) {
    audio.dataset.record = id;
    audio.src = `/api/history/${id}/audio`;
  }
  listeningSegments = body.segments || [];
  list.replaceChildren();
  document.querySelector('#listening-status').textContent = listeningSegments.length ? '' : 'Речь в записи не обнаружена.';
  const labels = new Map((body.speakers || []).map(speaker => [speaker.id, body.names?.[speaker.id] || speaker.label]));
  for (const segment of listeningSegments) {
    const row = element('li');
    const button = element('button', 'segment-button');
    button.type = 'button';
    button.append(element('span', 'segment-time', `▶ ${timeLabel(segment.start)}`));
    const content = element('span', 'segment-content');
    if (labels.has(segment.speaker)) content.append(element('strong', 'segment-speaker', labels.get(segment.speaker)));
    content.append(element('span', '', segment.text));
    button.append(content);
    button.onclick = () => {
      document.querySelector('#listening-status').textContent = '';
      audio.currentTime = Number.isFinite(audio.duration) ? Math.min(segment.start, Math.max(0, audio.duration - .05)) : segment.start;
      audio.play().catch(() => {
        if (viewingId === id) document.querySelector('#listening-status').textContent = 'Не удалось начать воспроизведение. Нажмите ▶ в плеере или повторите выбор реплики.';
      });
    };
    row.append(button); list.append(row);
  }
  highlightSegment();
}
function highlightSegment() {
  const audio = document.querySelector('#transcript-audio');
  const time = audio.currentTime;
  let active = -1;
  for (let i = 0; i < listeningSegments.length; i++) {
    if (time >= listeningSegments[i].start && time < listeningSegments[i].end) active = i;
  }
  document.querySelectorAll('#transcript-segments button').forEach((button, i) => {
    if (i === active) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  });
}
document.querySelector('#transcript-audio').addEventListener('timeupdate', highlightSegment);
document.querySelector('#transcript-audio').addEventListener('error', () => {
  if (viewingId && !document.querySelector('#listening').hidden) document.querySelector('#listening-status').textContent = 'Не удалось загрузить аудио. Проверьте связь с сервером и наличие файла в истории.';
});

function updateSpeakerControls() {
  const active = form.elements.mode.value === 'transcribe';
  const checkbox = form.elements.diarize;
  checkbox.disabled = !active || !speakerState.ready;
  if (!speakerState.ready) checkbox.checked = false;
  const count = document.querySelector('#speaker-count-option');
  count.hidden = !checkbox.checked || !active;
  form.elements.num_speakers.disabled = count.hidden;
}
form.elements.diarize.addEventListener('change', updateSpeakerControls);
async function refreshSpeakerStatus() {
  try {
    speakerState = await api('/api/diarization/status');
    updateSpeakerControls();
    const status = document.querySelector('#speaker-model-status');
    status.textContent = speakerState.error || (speakerState.preparing ? 'Скачиваю и проверяю модель… Это может занять несколько минут.'
      : speakerState.ready ? 'Готово · разделение по говорящим на CPU, локально'
      : !speakerState.libraries_ready ? 'Для первого запуска установите библиотеки: setup-speakers.cmd'
      : 'Библиотеки готовы. Скачайте модель в настройке ниже.');
    document.querySelector('#prepare-speakers').disabled = speakerState.preparing || !speakerState.libraries_ready;
    document.querySelector('#model-access-controls').hidden = speakerState.ready;
  } catch { document.querySelector('#speaker-model-status').textContent = 'Не удалось проверить модель. Проверьте соединение с сервером.'; }
}
document.querySelector('#prepare-speakers').onclick = async () => {
  const input = document.querySelector('#hf-model-token');
  const token = input.value.trim();
  if (!token) { document.querySelector('#speaker-model-status').textContent = 'Введите токен Hugging Face в поле настройки.'; return; }
  const button = document.querySelector('#prepare-speakers');
  button.disabled = true;
  try {
    await api('/api/diarization/prepare', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token})});
    input.value = '';
    await refreshSpeakerStatus();
  } catch (error) { document.querySelector('#speaker-model-status').textContent = error.message; }
  finally { input.value = ''; button.disabled = speakerState.preparing || !speakerState.libraries_ready; }
};
function renderSpeakerNames(body) {
  currentSpeakers = body.speakers || [];
  const panel = document.querySelector('#speaker-names');
  panel.hidden = !currentSpeakers.length;
  const rows = document.querySelector('#speaker-name-rows');
  rows.replaceChildren();
  document.querySelector('#speaker-name-status').textContent = '';
  for (const speaker of currentSpeakers) {
    const row = element('tr');
    row.append(element('td','',speaker.label));
    const cell = element('td');
    const input = element('input');
    input.type = 'text'; input.maxLength = 80;
    input.dataset.speaker = speaker.id;
    input.value = body.names?.[speaker.id] || '';
    input.placeholder = speaker.label;
    input.setAttribute('aria-label', `Имя для ${speaker.label}`);
    cell.append(input); row.append(cell); rows.append(row);
  }
}
document.querySelector('#save-speaker-names').onclick = async () => {
  if (!viewingId) return;
  const id = viewingId;
  const names = {};
  for (const input of document.querySelectorAll('#speaker-name-rows input')) names[input.dataset.speaker] = input.value.trim();
  const button = document.querySelector('#save-speaker-names');
  button.disabled = true;
  try {
    const body = await api(`/api/history/${id}/speakers`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({names})});
    if (viewingId !== id) return;
    document.querySelector('#transcript').value = body.text;
    renderListening(body, id);
    document.querySelector('#speaker-name-status').textContent = 'Имена сохранены. Скачиваемый TXT и ZIP используют эти имена.';
    // Force a fresh download URL even if the browser cached an older TXT.
    const download = document.querySelector('#download');
    download.href = `/api/history/${id}/download?v=${Date.now()}`;
    await refreshHistory();
  } catch (error) { if (viewingId === id) document.querySelector('#speaker-name-status').textContent = error.message; }
  finally { button.disabled = false; }
};
