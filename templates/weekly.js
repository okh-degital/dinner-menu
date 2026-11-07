(() => {
  const meals = document.querySelector('.meals');
  const cards = Array.from(meals.querySelectorAll('article'));
  const list = document.querySelector('.checklist');
  const boxes = () => Array.from(list.querySelectorAll('input'));
  const payload = document.getElementById('redraw');
  const plan = JSON.parse(payload.textContent);
  const status = document.getElementById('change-status');
  const legacyKey = 'dinner-shopping-v1:' + meals.textContent.replace(/\s+/g, ' ').trim()
    + JSON.stringify(boxes().map(box => box.dataset.key));
  const key = plan ? 'dinner-week-v1:' + payload.dataset.edition : legacyKey;
  const selected = plan ? plan.original.slice() : [];

  function storageWarning() {
    document.getElementById('save-status').textContent =
      'このブラウザでは保存できません。変更とチェックはこのページを開いている間だけ有効です。';
  }

  function checkedItems() {
    return new Set(boxes().filter(box => box.checked).map(box => box.parentElement.textContent));
  }

  function save() {
    try {
      const state = plan ? {selected, checked: Array.from(checkedItems())}
        : Object.fromEntries(boxes().map(box => [box.dataset.key, box.checked]));
      localStorage.setItem(key, JSON.stringify(state));
    } catch { storageWarning(); }
  }

  // Amounts are integer multiples of the common denominator calculated by Python.
  function number(ticks, scale) {
    const whole = ticks / scale;
    const rest = ticks % scale;
    if (!rest) return String(whole);
    let a = rest, b = scale;
    while (b) [a, b] = [b, a % b];
    return (whole ? whole + 'と' : '') + (rest / a) + '/' + (scale / a);
  }

  function amountText(unit, ticks, scale) {
    if (unit === '小さじ' && ticks >= 3n * scale) {
      const remainder = ticks % (3n * scale);
      return '大さじ' + (ticks / (3n * scale)) + (remainder ? '＋小さじ' + number(remainder, scale) : '');
    }
    const value = number(ticks, scale);
    if (unit === '茶碗杯') return '茶碗' + value + '杯';
    return unit === '小さじ' ? unit + value : value + unit;
  }

  function renderShopping(checked) {
    const totals = new Map();
    selected.forEach((id, index) => {
      for (const item of plan.catalog[id].ingredients) {
        if (!totals.has(item.name)) totals.set(item.name, {amounts: new Map(), approx: false, unknown: []});
        const total = totals.get(item.name);
        for (const [unit, ticks] of Object.entries(item.amounts)) {
          total.amounts.set(unit, (total.amounts.get(unit) || 0n) + BigInt(ticks));
        }
        total.approx ||= item.approx;
        for (const note of item.unknown) total.unknown.push(plan.days[index] + '：' + note);
      }
    });
    const rows = document.createDocumentFragment();
    for (const [name, total] of totals) {
      const parts = Array.from(total.amounts, ([unit, ticks]) => amountText(unit, ticks, BigInt(plan.denominator)));
      let display = parts.join('＋') + (total.approx ? '（目安）' : '');
      if (total.unknown.length) display += (display ? '＋' : '') + '不明';
      const row = document.createElement('li');
      const label = document.createElement('label');
      const box = document.createElement('input');
      box.type = 'checkbox';
      const span = document.createElement('span');
      span.textContent = name + '：' + display;
      if (total.unknown.length) {
        const note = document.createElement('small');
        note.className = 'note';
        note.textContent = total.unknown.join(' / ');
        span.append(note);
      }
      box.checked = checked.has(span.textContent);
      label.append(box, span);
      row.append(label);
      rows.append(row);
    }
    list.replaceChildren(rows);
  }

  function fits(id, index, ids) {
    const rules = plan.catalog[id].rules[index];
    return Object.entries(rules).every(([rule, remaining]) => {
      const used = ids.filter((other, day) => day !== index &&
        Object.hasOwn(plan.catalog[other].rules[day], rule)).length;
      return used < remaining;
    });
  }

  function choose(index) {
    const others = selected.filter((_, day) => day !== index).map(id => plan.catalog[id]);
    const options = Object.entries(plan.catalog).filter(([id]) =>
      id !== plan.original[index] && !selected.includes(id) &&
      !plan.excluded[index].includes(id) && fits(id, index, selected));
    if (!options.length) return null;
    // Favour variety, existing ingredients and highly rated recipes, while keeping every eligible dish drawable.
    const ingredients = new Set(others.flatMap(entry => entry.ingredients.map(item => item.name)));
    const primary = others.flatMap(entry => entry.primary);
    const weights = options.map(([, entry]) => {
      const variety = others.some(other => other.group === entry.group) ? 1 : 2;
      const overuse = entry.primary.some(name => primary.filter(item => item === name).length >= plan.mainLimit);
      const shared = entry.ingredients.filter(item => ingredients.has(item.name)).length;
      return variety * (overuse ? 0.5 : 1) * (1 + Math.min(shared, 4) / 4) * (1 + Math.max(0, entry.score));
    });
    let draw = Math.random() * weights.reduce((sum, weight) => sum + weight, 0);
    for (let i = 0; i < options.length; i++) {
      draw -= weights[i];
      if (draw < 0) return options[i][0];
    }
    return options[options.length - 1][0];
  }

  function renderRecipe(index) {
    // Card HTML is escaped by the same Python renderer used for the initial page.
    cards[index].querySelector('.recipe').innerHTML = plan.catalog[selected[index]].body;
  }

  const share = document.getElementById('share');
  const shareInput = document.getElementById('share-url');
  const shareStatus = document.getElementById('share-status');
  const params = new URLSearchParams(location.search);

  function validSelection(ids) {
    return plan && Array.isArray(ids) && ids.length === selected.length &&
      new Set(ids).size === ids.length &&
      ids.every(id => typeof id === 'string' && Object.hasOwn(plan.catalog, id)) &&
      ids.every((id, i) => id === plan.original[i] ||
        (!plan.excluded[i].includes(id) && fits(id, i, ids)));
  }

  function refreshShare() {
    const url = new URL(share.dataset.publicUrl);
    const marker = document.querySelector('meta[name="dinner-build"]')?.content;
    if (marker) url.searchParams.set('update', marker.slice(0, 12));
    url.searchParams.set('edition', payload.dataset.edition);
    url.searchParams.set('menu', selected.join(','));
    shareInput.value = url.href;
    shareStatus.textContent = '';
    const code = DinnerQRCode(url.href);
    const canvas = document.getElementById('share-qr');
    const cell = 6, margin = 4, size = code.getModuleCount();
    canvas.width = canvas.height = (size + margin * 2) * cell;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'white';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'black';
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (code.isDark(y, x)) ctx.fillRect((x + margin) * cell, (y + margin) * cell, cell, cell);
      }
    }
  }

  async function copyShare() {
    try {
      await navigator.clipboard.writeText(shareInput.value);
      shareStatus.textContent = 'コピーしました。';
    } catch {
      shareInput.hidden = false;
      shareInput.focus();
      shareInput.select();
      shareStatus.textContent = '選択したURLをコピーしてください。';
    }
  }

  function change(index) {
    const id = choose(index);
    if (id === null) {
      status.textContent = plan.days[index] + '曜日は、条件に合う別の候補がありません。';
      return;
    }
    const checked = checkedItems();
    selected[index] = id;
    renderRecipe(index);
    renderShopping(checked);
    save();
    refreshShare();
    // Keep an edited, imported menu on reload. Opening the original QR still imports its snapshot.
    if (params.has('menu')) {
      const here = new URL(location.href);
      here.searchParams.set('edition', payload.dataset.edition);
      here.searchParams.set('menu', selected.join(','));
      try { history.replaceState(null, '', here); } catch { /* Some file viewers restrict URL changes. */ }
    }
    status.textContent = plan.days[index] + '曜日を「' + cards[index].querySelector('h2').textContent + '」に変更しました。';
  }

  let saved;
  try {
    saved = plan && JSON.parse(localStorage.getItem(key));
    if (!saved) {
      const legacy = JSON.parse(localStorage.getItem(legacyKey) || '{}');
      boxes().forEach(box => { box.checked = legacy?.[box.dataset.key] === true; });
    }
  } catch { storageWarning(); }

  const hasMenu = params.has('menu');
  const sharedIds = hasMenu ? params.get('menu').split(',') : null;
  const imported = params.getAll('menu').length === 1 && params.getAll('edition').length === 1 &&
    params.get('edition') === payload.dataset.edition && validSelection(sharedIds);
  const initial = imported ? sharedIds : saved?.selected;
  if (validSelection(initial)) {
    const checks = Array.isArray(saved?.checked) ? new Set(saved.checked) : checkedItems();
    selected.splice(0, selected.length, ...initial);
    selected.forEach((id, i) => { if (id !== plan.original[i]) renderRecipe(i); });
    renderShopping(checks);
    if (imported) save();
  }

  if (hasMenu && !imported) {
    status.textContent = '献立の更新またはURLの誤りにより、共有された4品を復元できません。この端末の献立を表示しています。';
  } else if (plan) {
    status.textContent = imported ? '共有された献立を表示しています。' : '変更した献立は、この端末・ブラウザに保存されます。';
  }

  if (plan) {
    share.hidden = false;
    share.open = matchMedia('(min-width: 720px)').matches;
    refreshShare();
    document.getElementById('copy-share').addEventListener('click', copyShare);
    cards.forEach((card, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'change-meal';
      button.textContent = 'この料理だけ変更';
      button.setAttribute('aria-label', plan.days[index] + '曜日のこの料理だけ変更');
      button.addEventListener('click', () => change(index));
      card.querySelector('.recipe').before(button);
    });
  }
  list.addEventListener('change', save);
  document.getElementById('reset').addEventListener('click', () => {
    boxes().forEach(box => { box.checked = false; });
    save();
  });
})();
