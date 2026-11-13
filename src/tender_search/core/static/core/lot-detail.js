// Background work updates read-only regions, never the user's price or note form.
(() => {
  const root = document.getElementById('lot-detail');
  if (!root) return;
  let timer, polling = false, posting = false, pending = root.dataset.pending === 'true', failures = 0, generation = 0;
  const revisions = {detail: root.dataset.detail, documents: root.dataset.documents, specification: root.dataset.specification};
  const error = document.createElement('p');
  error.className = 'error'; error.hidden = true; error.setAttribute('role', 'status');
  root.append(error);

  function replaceRegion(id, source) {
    const current = document.getElementById(id), next = source.querySelector('#' + id);
    if (!current || !next) return;
    if (id === 'lot-specification') {
      // Keep open documents and their internal scroll position, including on retry.
      for (const detail of next.querySelectorAll('details[data-document]')) {
        const old = [...current.querySelectorAll('details[data-document]')].find(item => item.dataset.document === detail.dataset.document);
        if (!old) continue;
        if (old.textContent === detail.textContent) {
          const text = old.querySelector('pre');
          if (text) text.dataset.restoreScroll = text.scrollTop;
          detail.replaceWith(old);
        }
        else {
          detail.open = old.open;
          const text = detail.querySelector('pre');
          if (text) text.dataset.restoreScroll = old.querySelector('pre')?.scrollTop || 0;
        }
      }
    }
    current.replaceWith(next);
    for (const text of next.querySelectorAll('[data-restore-scroll]')) text.scrollTop = Number(text.dataset.restoreScroll);
    window.htmx?.process(next);
  }

  function update(data) {
    if (data.html) {
      const source = new DOMParser().parseFromString(data.html, 'text/html');
      // Anchor to the panel being read, so changes above it don't move the viewport.
      const anchor = [...root.querySelectorAll(':scope > *, #lot-header')].find(item => item.getBoundingClientRect().bottom > 85);
      const top = anchor?.getBoundingClientRect().top;
      if (data.revisions.detail !== revisions.detail) {
        replaceRegion('lot-header', source);
        replaceRegion('lot-description', source);
        const price = document.getElementById('lot-price-region');
        if (price && !price.querySelector('form')) replaceRegion('lot-price-region', source);
      }
      if (data.revisions.documents !== revisions.documents) replaceRegion('lot-documents', source);
      if (data.revisions.specification !== revisions.specification) replaceRegion('lot-specification', source);
      const restored = anchor?.isConnected ? anchor : document.getElementById(anchor?.id);
      if (restored && top !== undefined) window.scrollBy({top: restored.getBoundingClientRect().top - top, behavior: 'instant'});
      Object.assign(revisions, data.revisions);
    }
    pending = data.pending;
    error.hidden = true;
  }

  function schedule(delay = 4000) {
    clearTimeout(timer);
    if (pending && !document.hidden) timer = setTimeout(poll, delay);
  }

  async function poll() {
    if (document.hidden) return;
    if (polling || posting) { schedule(); return; }
    polling = true;
    const requestedGeneration = generation;
    const url = new URL(root.dataset.statusUrl, location.origin);
    for (const [key, value] of Object.entries(revisions)) url.searchParams.set(key, value);
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(20000)});
      if (!response.ok || response.redirected) throw new Error('status');
      const data = await response.json();
      if (String(data.owner) !== document.body.dataset.userId) {
        pending = false;
        error.textContent = 'Учётная запись изменилась. Откройте карточку заново.';
        error.hidden = false;
        return;
      }
      if (!posting && requestedGeneration === generation) update(data);
      failures = 0;
    } catch (_) {
      failures++;
      if (failures >= 3) {
        error.textContent = 'Не удалось получить состояние загрузки. Повторяем подключение…';
        error.hidden = false;
      }
    } finally {
      polling = false;
      schedule(Math.min(30000, 4000 * (failures + 1)));
    }
  }

  root.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('form[data-lot-async]')) return;
    event.preventDefault();
    if (posting) return;
    posting = true;
    generation++;
    const button = form.querySelector('button[type=submit]'), original = button?.innerHTML;
    if (button) {
      button.disabled = true;
      button.innerHTML = '<span class="search-spinner" aria-hidden="true"></span> ' + (form.closest('#lot-specification') ? 'Извлекаем…' : 'Загружаем…');
    }
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin', headers: {'X-Lot-Async': '1'}, signal: AbortSignal.timeout(20000)});
      if (!response.ok || response.redirected) throw new Error('submit');
      const data = await response.json();
      if (data.specification_html) {
        replaceRegion('lot-specification', new DOMParser().parseFromString(data.specification_html, 'text/html'));
      }
      pending = true;
      // Force a fresh region even if a duplicate request found an existing job.
      if (form.closest('#lot-specification')) revisions.specification = '';
      else if (form.closest('#lot-documents')) revisions.documents = '';
      else revisions.detail = '';
    } catch (_) {
      if (button) { button.disabled = false; button.innerHTML = original; }
      error.textContent = 'Не удалось подтвердить запуск. Проверьте соединение и повторите попытку.';
      error.hidden = false;
    } finally {
      posting = false;
      schedule(0);
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(timer);
    else schedule(0);
  });
  schedule();
})();
