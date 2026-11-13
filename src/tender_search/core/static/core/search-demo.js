// Draft input and applied results are separate; only the search button applies.
(() => {
  const root = document.querySelector('[data-search-demo]');
  if (!root) return;
  const examples = {
    laptop: {term: 'Ноутбук', suffix: ' для офиса', category: 'Техника и оборудование', second: 'Компьютерное оборудование', snippet: ', 16 ГБ…'},
    chair: {term: 'Кресло', suffix: ' офисное', category: 'Мебель', second: 'Мебель для кабинета', snippet: ' с регулировкой высоты…'},
    paper: {term: 'Бумага', suffix: ' для печати А4', category: 'Канцелярские товары', second: 'Офисные расходные материалы', snippet: ' А4, 80 г/м²…'},
    printer: {term: 'Принтер', suffix: ' лазерный', category: 'Оргтехника', second: 'Оборудование для печати', snippet: ' с двусторонней печатью…'},
    milk: {term: 'Молоко', suffix: ' питьевое', category: 'Продукты питания', second: 'Продукты для столовой', snippet: ' пастеризованное, 2,5%…'},
    gloves: {term: 'Перчатки', suffix: ' медицинские', category: 'Медицинские изделия', second: 'Медицинские расходные материалы', snippet: ' нитриловые, размер М…'},
  };
  const input = root.querySelector('[data-demo-query]');
  const form = root.querySelector('[data-demo-form]');
  const list = root.querySelector('.demo-options');
  const empty = root.querySelector('[data-demo-empty]');
  const error = root.querySelector('.demo-query-error');
  const choices = Object.values(examples);
  const normalize = (value) => value.trim().toLocaleLowerCase('ru');
  let filtered = [];
  let active = -1;

  function close() {
    list.hidden = true;
    empty.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    active = -1;
  }
  function clearError() {
    error.hidden = true;
    error.textContent = '';
    input.removeAttribute('aria-invalid');
  }
  function choose(item) {
    input.value = item.term.toLowerCase();
    clearError();
    close();
    input.focus();
  }
  function suggest() {
    const query = normalize(input.value);
    filtered = choices.filter(item => normalize(item.term).includes(query));
    active = -1;
    input.removeAttribute('aria-activedescendant');
    list.replaceChildren();
    filtered.forEach((item, index) => {
      const option = document.createElement('li');
      option.id = `demo-option-${index}`;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', 'false');
      const term = item.term.toLowerCase();
      const start = term.indexOf(query);
      if (query) {
        const mark = document.createElement('mark');
        mark.textContent = term.slice(start, start + query.length);
        option.append(document.createTextNode(term.slice(0, start)), mark,
          document.createTextNode(term.slice(start + query.length)));
      } else {
        option.textContent = term;
      }
      // Keep focus in the combobox so a pointer choice does not blur it first.
      option.addEventListener('pointerdown', event => event.preventDefault());
      option.addEventListener('click', () => choose(item));
      list.append(option);
    });
    list.hidden = filtered.length === 0;
    empty.hidden = filtered.length !== 0;
    input.setAttribute('aria-expanded', String(filtered.length > 0));
  }
  input.addEventListener('input', () => { clearError(); suggest(); });
  input.addEventListener('focus', suggest);
  input.addEventListener('click', suggest);
  input.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Escape') { close(); event.preventDefault(); }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (list.hidden) suggest();
      if (!filtered.length) return;
      active = event.key === 'ArrowDown' ? (active + 1) % filtered.length
        : (active <= 0 ? filtered.length - 1 : active - 1);
      [...list.children].forEach((option, index) => option.setAttribute('aria-selected', String(index === active)));
      input.setAttribute('aria-activedescendant', list.children[active].id);
      list.children[active].scrollIntoView({block: 'nearest'});
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (!list.hidden && active >= 0) choose(filtered[active]);
      else if (!list.hidden && filtered.length === 1) choose(filtered[0]);
      // Enter chooses a suggestion. The search button applies it separately.
    }
  });
  form.addEventListener('focusout', event => {
    if (!form.contains(event.relatedTarget)) close();
  });
  document.addEventListener('click', event => {
    if (!form.contains(event.target)) close();
  });

  function update(item) {
    const titleMark = document.createElement('mark');
    titleMark.textContent = item.term;
    root.querySelector('[data-demo-title]').replaceChildren(titleMark, document.createTextNode(item.suffix));
    root.querySelector('[data-demo-description]').textContent = `Найдено «${item.term.toLowerCase()}» в названии и описании`;
    root.querySelector('[data-demo-category]').textContent = item.category;
    root.querySelector('[data-demo-secondary-title]').textContent = item.second;
    const snippetMark = document.createElement('mark');
    snippetMark.textContent = item.term.toLowerCase();
    root.querySelector('[data-demo-snippet]').replaceChildren(
      document.createTextNode('В техспецификации: «'), snippetMark, document.createTextNode(item.snippet + '»'));
  }
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const item = choices.find(candidate => normalize(candidate.term) === normalize(input.value));
    if (!item) {
      error.textContent = 'Выберите доступный запрос из подсказок: например, ноутбук или кресло.';
      error.hidden = false;
      input.setAttribute('aria-invalid', 'true');
      input.focus();
      suggest();
      return;
    }
    clearError();
    close();
    update(item);
  });
})();
