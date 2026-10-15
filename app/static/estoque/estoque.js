'use strict';

const API = '/api/estoque';
const CSRF_HEADER = { 'X-Requested-With': 'estoque-panel' };
const REFRESH_MS = 30000;
const LOW_STOCK = 5;
const TIMEZONE = 'America/Recife';

const TYPE_LABELS = {
  INITIAL_STOCK: 'Estoque inicial',
  RESTOCK: 'Reposição',
  MANUAL_SALE: 'Venda manual',
  LOSS: 'Perda/avaria',
  ADJUSTMENT: 'Ajuste',
  MARKETPLACE_SALE: 'Marketplace',
  UNTRACKED: 'Sem contexto'
};

const MODES = {
  out: {
    eyebrow: 'Saída de estoque',
    submit: 'Registrar saída',
    danger: true,
    types: [
      { value: 'MANUAL_SALE', title: 'Venda', hint: 'alguém levou e pagou' },
      { value: 'LOSS', title: 'Perda', hint: 'avaria, vencida, sumiu' }
    ]
  },
  in: {
    eyebrow: 'Entrada de estoque',
    submit: 'Registrar entrada',
    danger: false,
    types: [{ value: 'RESTOCK', title: 'Reposição', hint: 'latas chegaram' }]
  },
  adjust: {
    eyebrow: 'Ajuste de inventário',
    submit: 'Salvar contagem',
    danger: false,
    types: [{ value: 'ADJUSTMENT', title: 'Contagem física', hint: 'corrige o estoque' }]
  }
};

const AUDIENCE_LABELS = {
  MEMBER: 'Membros',
  NON_MEMBER: 'Não membros'
};
// Descontos por lata sobre o preço base, em centavos (mesma regra de app/price_service.py).
const PRICE_RULE = [
  { min: 1, max: 1, discount: 0 },
  { min: 2, max: 3, discount: 50 },
  { min: 4, max: null, discount: 100 }
];

const state = {
  user: null,
  products: [],
  prices: [],
  savingPrice: false,
  category: '',
  search: '',
  history: [],
  historyDirection: '',
  historyHasMore: false,
  dialog: null,
  submitting: false,
  refreshTimer: null
};

const el = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
const money = value => new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(value);
const units = n => `${n} ${Math.abs(n) === 1 ? 'lata' : 'latas'}`;
const timeFormat = new Intl.DateTimeFormat('pt-BR', { timeZone: TIMEZONE, hour: '2-digit', minute: '2-digit' });
const dayFormat = new Intl.DateTimeFormat('pt-BR', { timeZone: TIMEZONE, day: '2-digit', month: '2-digit' });
const dayKey = new Intl.DateTimeFormat('en-CA', { timeZone: TIMEZONE });

function formatWhen(iso) {
  const date = new Date(iso);
  const sameDay = dayKey.format(date) === dayKey.format(new Date());
  return sameDay ? timeFormat.format(date) : `${dayFormat.format(date)} ${timeFormat.format(date)}`;
}

/* ---------------------------------------------------------------- API ---- */

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function errorMessage(data) {
  if (!data || data.detail == null) return 'Não foi possível concluir a operação. Tente novamente.';
  if (Array.isArray(data.detail)) {
    return data.detail.map(item => String(item.msg || '').replace(/^Value error, /, '')).filter(Boolean).join(' ') || 'Dados inválidos.';
  }
  return String(data.detail);
}

async function api(path, options = {}) {
  const method = options.method || 'GET';
  const headers = { Accept: 'application/json', ...(method === 'GET' ? {} : { 'Content-Type': 'application/json', ...CSRF_HEADER }) };
  let response;
  try {
    response = await fetch(`${API}${path}`, { credentials: 'same-origin', ...options, method, headers });
  } catch {
    throw new ApiError('Sem conexão com o servidor. Verifique a rede.', 0);
  }
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && path !== '/login' && path !== '/me') showLogin('Sua sessão expirou. Entre novamente.');
    throw new ApiError(errorMessage(data), response.status);
  }
  return data;
}

/* -------------------------------------------------------------- Toast ---- */

let toastTimer;
function toast(message, isError = false) {
  const node = el('toast');
  clearTimeout(toastTimer);
  node.textContent = message;
  node.classList.toggle('error', isError);
  node.classList.add('show');
  toastTimer = setTimeout(() => node.classList.remove('show'), 3800);
}

/* ------------------------------------------------------------ Imagens ---- */
// Mesmo mapeamento do PDV (app/static/app.js) para os sabores aparecerem iguais.

function normalizeId(value) {
  return String(value || '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

function productImage(product) {
  const images = {
    tropical: '/static/assets/red-bull-tropical-transparent.png?v=20260802-2',
    melancia: '/static/assets/red-bull-melancia-transparent.png?v=20260802-2',
    original: '/static/assets/red-bull-original-transparent.png?v=20260802-2',
    zero_tradicional: '/static/assets/redbull_tradicional_zero.png?v=20260806-2',
    redbull_tradicional_zero: '/static/assets/redbull_tradicional_zero.png?v=20260806-2',
    redbull_zero: '/static/assets/redbull_tradicional_zero.png?v=20260806-2',
    nectarina: '/static/assets/nectarina.png?v=20260806-2',
    nectarina_sugarfree: '/static/assets/nectarina_sugarfree-removebg.png?v=20260806-2',
    pomelo_sugarfree: '/static/assets/pomelo_sugarfree-removebg-preview.png?v=20260806-2',
    amora_sugarfree: '/static/assets/amora_sugarfree.png?v=20260806-2',
    maca_sugarfree: '/static/assets/maça_sugarfree.png?v=20260806-2',
    melao_e_maracuja: '/static/assets/Melão_e_maracuja.webp?v=20260806-2',
    morango_e_pessego: '/static/assets/morango_e_pessego.webp?v=20260806-2',
    sugarfree: '/static/assets/sugarfree-removebg.png?v=20260806-2',
    tradicional_sugarfree: '/static/assets/sugarfree-removebg.png?v=20260806-2'
  };

  const id = normalizeId(product.id);
  if (images[id]) return images[id];

  const name = normalizeId(product.name);
  if (name.includes('zero') || name.includes('tradicional')) return images.zero_tradicional;
  if (name.includes('nectarina') && name.includes('sugarfree')) return images.nectarina_sugarfree;
  if (name.includes('nectarina')) return images.nectarina;
  if (name.includes('pomelo')) return images.pomelo_sugarfree;
  if (name.includes('amora')) return images.amora_sugarfree;
  if (name.includes('maca')) return images.maca_sugarfree;
  if (name.includes('morango') || name.includes('pessego')) return images.morango_e_pessego;
  if (name.includes('melao') || name.includes('maracuja')) return images.melao_e_maracuja;
  if (name.includes('sugarfree')) return images.sugarfree;
  return images.original;
}

/* ------------------------------------------------------------ Login ------ */

function showLogin(message = '') {
  stopRefresh();
  state.user = null;
  if (el('movement-dialog').open) el('movement-dialog').close();
  el('panel-view').classList.add('hidden');
  el('login-view').classList.remove('hidden');
  el('login-error').textContent = message;
  el('login-password').value = '';
  setTimeout(() => (el('login-username').value ? el('login-password') : el('login-username')).focus(), 50);
}

function showPanel(user) {
  state.user = user;
  el('login-view').classList.add('hidden');
  el('panel-view').classList.remove('hidden');
  el('user-name').textContent = user.full_name;
  el('user-role').textContent = user.role === 'ADMIN' ? 'Administrador' : 'Operador';
  // Os cartões de preço dependem do perfil (admin edita, operador só vê).
  el('price-tables').innerHTML = '';
  refreshAll();
  startRefresh();
}

async function onLogin(event) {
  event.preventDefault();
  const username = el('login-username').value.trim().toLowerCase();
  const password = el('login-password').value;
  if (!username || !password) {
    el('login-error').textContent = 'Informe usuário e senha.';
    return;
  }
  const button = el('login-submit');
  button.disabled = true;
  button.classList.add('loading');
  el('login-error').textContent = '';
  try {
    const user = await api('/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    el('login-password').value = '';
    showPanel(user);
  } catch (error) {
    el('login-error').textContent = error.message;
    el('login-password').select();
  } finally {
    button.disabled = false;
    button.classList.remove('loading');
  }
}

async function onLogout() {
  try {
    await api('/logout', { method: 'POST' });
  } catch {
    // Mesmo que a rede falhe, a tela volta para o login.
  }
  showLogin('Você saiu do painel.');
}

/* ------------------------------------------------------------ Dados ------ */

async function refreshAll({ automatic = false } = {}) {
  const tasks = [loadProducts(), loadSummary(), loadPrices()];
  // No refresh automático, não descarta páginas carregadas em "Carregar mais".
  if (!automatic || state.history.length <= HISTORY_PAGE) tasks.push(loadHistory());
  await Promise.allSettled(tasks);
}

async function loadProducts() {
  try {
    state.products = await api('/produtos');
    renderCategories();
    renderProducts();
  } catch (error) {
    if (error.status !== 401) toast(error.message, true);
  } finally {
    el('products').setAttribute('aria-busy', 'false');
  }
}

async function loadSummary() {
  try {
    const summary = await api('/resumo');
    el('kpi-out').textContent = summary.units_out_today;
    el('kpi-out-detail').textContent = `${summary.marketplace_sales_today} marketplace · ${summary.manual_sales_today} manual · ${summary.losses_today} perda`;
    el('kpi-in').textContent = summary.units_in_today;
    el('kpi-stock').textContent = summary.total_stock;
    el('kpi-available').textContent = summary.total_available;
    el('kpi-reserved').textContent = summary.total_reserved
      ? `${units(summary.total_reserved)} reservadas em PIX`
      : 'nenhuma reserva PIX aberta';
  } catch (error) {
    if (error.status !== 401) toast(error.message, true);
  }
}

async function loadPrices() {
  try {
    state.prices = await api('/precos');
    renderPrices();
  } catch (error) {
    if (error.status !== 401) toast(error.message, true);
  }
}

const HISTORY_PAGE = 30;
let historyRequest = 0;

async function loadHistory(append = false) {
  // Só a resposta da requisição mais recente é aplicada (cliques rápidos nos
  // filtros ou um refresh automático não embaralham a lista).
  const requestId = ++historyRequest;
  const params = new URLSearchParams({ limit: String(HISTORY_PAGE) });
  if (state.historyDirection) params.set('direction', state.historyDirection);
  if (append && state.history.length) params.set('before_id', state.history[state.history.length - 1].id);
  try {
    const page = await api(`/movimentos?${params}`);
    if (requestId !== historyRequest) return;
    state.history = append ? state.history.concat(page) : page;
    state.historyHasMore = page.length === HISTORY_PAGE;
    renderHistory();
  } catch (error) {
    if (requestId === historyRequest && error.status !== 401) toast(error.message, true);
  }
}

function startRefresh() {
  stopRefresh();
  state.refreshTimer = setInterval(() => {
    if (document.visibilityState !== 'visible' || el('movement-dialog').open || !state.user) return;
    refreshAll({ automatic: true });
  }, REFRESH_MS);
}

function stopRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = null;
}

/* ------------------------------------------------------------ Render ----- */

function renderCategories() {
  const categories = [...new Set(state.products.map(product => product.category))].sort();
  if (state.category && !categories.includes(state.category)) state.category = '';
  const chips = [['', 'Todos'], ...categories.map(category => [category, category])];
  el('category-filters').innerHTML = chips.map(([value, label]) => (
    `<button type="button" class="chip${state.category === value ? ' active' : ''}" data-category="${escapeHtml(value)}">${escapeHtml(label)}</button>`
  )).join('');
}

function stockBadge(product) {
  if (product.available <= 0) return '<span class="badge empty">Esgotado no marketplace</span>';
  if (product.available <= LOW_STOCK) return `<span class="badge low">Estoque baixo</span>`;
  return `<span class="badge">${escapeHtml(product.category)}</span>`;
}

function productCard(product) {
  const isAdmin = state.user && state.user.role === 'ADMIN';
  const reserved = product.reserved_stock
    ? `<span><strong>${product.reserved_stock}</strong> reservada(s) PIX</span>`
    : '';
  return `
    <article class="card" data-id="${escapeHtml(product.id)}">
      <div class="card-top">
        <div class="card-image"><img src="${productImage(product)}" alt="" loading="lazy"></div>
        <div class="card-title">
          <h3>${escapeHtml(product.name)}</h3>
          ${stockBadge(product)}
        </div>
      </div>
      <div class="card-stock">
        <div class="stock-number">
          <b>${product.stock}</b>
          <small>${product.stock === 1 ? 'lata em estoque' : 'latas em estoque'}</small>
        </div>
        <div class="stock-meta">
          <span><strong>${product.available}</strong> no marketplace</span>
          ${reserved}
          <span>saíram hoje: <strong>${product.out_today}</strong></span>
        </div>
      </div>
      <div class="card-actions">
        <button type="button" class="btn-out" data-action="out" ${product.available <= 0 ? 'disabled' : ''} aria-label="Registrar saída de ${escapeHtml(product.name)}"><span class="sign" aria-hidden="true">−</span> Saída</button>
        <button type="button" class="btn-in" data-action="in" aria-label="Registrar entrada de ${escapeHtml(product.name)}"><span class="sign" aria-hidden="true">+</span> Entrada</button>
      </div>
      ${isAdmin ? `<button type="button" class="adjust-link" data-action="adjust">Ajustar contagem</button>` : ''}
    </article>`;
}

function renderProducts() {
  const term = normalizeId(state.search);
  const visible = state.products.filter(product => (
    (!state.category || product.category === state.category)
    && (!term || normalizeId(product.name).includes(term) || normalizeId(product.id).includes(term))
  ));
  el('products').innerHTML = visible.length
    ? visible.map(productCard).join('')
    : `<p class="empty-state">${state.products.length ? 'Nenhum sabor encontrado com esse filtro.' : 'Nenhum sabor cadastrado ainda. Rode sql/seed_sabores.sql para cadastrar o catálogo.'}</p>`;
}

function tierLabel(min, max) {
  if (max == null) return min === 1 ? 'Preço por lata' : `${min} ou mais`;
  if (min === max) return units(min);
  if (max === min + 1) return `${min} ou ${max} latas`;
  return `${min} a ${max} latas`;
}

function tiersHtml(tiers) {
  if (!tiers.length) return '<li class="price-empty">Nenhuma faixa ativa: este público não consegue comprar.</li>';
  return tiers.map(tier => (
    `<li><span>${tierLabel(tier.min_quantity, tier.max_quantity)}</span><b>${money(tier.unit_price)}</b></li>`
  )).join('');
}

function priceCard(table) {
  const audience = escapeHtml(table.pricing_audience);
  const label = AUDIENCE_LABELS[table.pricing_audience] || audience;
  const isAdmin = state.user && state.user.role === 'ADMIN';
  const form = isAdmin ? `
      <form class="price-form" data-audience="${audience}" novalidate>
        <label for="price-${audience}">Novo preço de 1 lata (x)</label>
        <div class="price-input">
          <span aria-hidden="true">R$</span>
          <input id="price-${audience}" inputmode="decimal" autocomplete="off" maxlength="7" placeholder="0,00">
        </div>
        <p class="preview" data-preview aria-live="polite"></p>
        <p class="form-error" data-error role="alert"></p>
        <button type="submit" class="primary-button">
          <span class="button-label">Salvar preço</span>
          <span class="button-loader" aria-hidden="true"></span>
        </button>
      </form>`
    : '<p class="muted small">Somente administradores alteram os preços.</p>';
  return `
    <article class="price-card" data-audience="${audience}">
      <h3>${escapeHtml(label)}</h3>
      <ul class="price-tiers"></ul>
      ${form}
    </article>`;
}

function renderPrices() {
  const container = el('price-tables');
  for (const table of state.prices) {
    let card = container.querySelector(`.price-card[data-audience="${CSS.escape(table.pricing_audience)}"]`);
    // O cartão é criado uma vez; o refresh automático só troca as faixas e
    // não apaga o que o administrador está digitando.
    if (!card) {
      container.insertAdjacentHTML('beforeend', priceCard(table));
      card = container.lastElementChild;
    }
    card.querySelector('.price-tiers').innerHTML = tiersHtml(table.tiers);
  }
}

function parseBasePrice(raw) {
  const value = String(raw || '').trim().replace(/^R\$\s*/i, '');
  if (!/^\d{1,4}([.,]\d{1,2})?$/.test(value)) return null;
  return Math.round(Number(value.replace(',', '.')) * 100);
}

function priceFromForm(form) {
  const cents = parseBasePrice(form.querySelector('input').value);
  if (cents == null) return { error: 'Informe o preço no formato 8,50.' };
  if (cents <= 100 && form.dataset.audience === 'MEMBER') return { error: 'O preço precisa ser maior que R$ 1,00 (a faixa de 4+ latas desconta R$ 1,00).' };
  if (cents <= 0) return { error: 'O preço precisa ser maior que zero.' };
  if (cents > 100000) return { error: 'O preço não pode passar de R$ 1.000,00.' };
  return { cents };
}

function syncPriceForm(form) {
  const input = form.querySelector('input');
  const preview = form.querySelector('[data-preview]');
  form.querySelector('[data-error]').textContent = '';
  input.removeAttribute('aria-invalid');
  const { cents } = priceFromForm(form);
  if (cents == null) {
    preview.innerHTML = '';
    return;
  }
  // A promoção por quantidade é só dos membros; não membros têm preço único, sem simulação.
  preview.innerHTML = form.dataset.audience === 'MEMBER'
    ? 'Vai ficar: ' + PRICE_RULE.map(rule => (
      `${tierLabel(rule.min, rule.max)} <b>${money((cents - rule.discount) / 100)}</b>`
    )).join(' · ')
    : '';
}

async function onSubmitPrice(event) {
  const form = event.target.closest('.price-form');
  if (!form) return;
  event.preventDefault();
  if (state.savingPrice) return;
  const input = form.querySelector('input');
  const errorNode = form.querySelector('[data-error]');
  const { cents, error } = priceFromForm(form);
  if (error) {
    input.setAttribute('aria-invalid', 'true');
    errorNode.textContent = error;
    return;
  }
  const audience = form.dataset.audience;
  const label = AUDIENCE_LABELS[audience] || audience;
  const confirmed = window.confirm(
    `Alterar o preço para ${label.toLowerCase()} para ${money(cents / 100)} (1 lata)? `
    + 'Vale para os próximos pedidos do marketplace; pedidos já abertos mantêm o preço antigo.'
  );
  if (!confirmed) return;

  const button = form.querySelector('button[type="submit"]');
  state.savingPrice = true;
  button.disabled = true;
  button.classList.add('loading');
  try {
    const table = await api('/precos', {
      method: 'PUT',
      body: JSON.stringify({ pricing_audience: audience, base_price: (cents / 100).toFixed(2) })
    });
    const index = state.prices.findIndex(item => item.pricing_audience === table.pricing_audience);
    if (index >= 0) state.prices[index] = table;
    renderPrices();
    input.value = '';
    syncPriceForm(form);
    toast(`Preço para ${label.toLowerCase()} atualizado: 1 lata ${money(table.base_price)}.`);
  } catch (err) {
    errorNode.textContent = err.message;
  } finally {
    state.savingPrice = false;
    button.disabled = false;
    button.classList.remove('loading');
  }
}

function historyItem(movement) {
  const direction = movement.delta < 0 ? 'out' : 'in';
  const who = [];
  if (movement.buyer_name) who.push(`comprador: <b>${escapeHtml(movement.buyer_name)}</b>`);
  if (movement.staff_name) who.push(`por ${escapeHtml(movement.staff_name)}`);
  if (movement.unit_price != null) who.push(money(movement.unit_price * movement.quantity));
  if (movement.note) who.push(`“${escapeHtml(movement.note)}”`);
  return `
    <li class="history-item">
      <span class="history-delta ${direction}">${movement.delta > 0 ? '+' : '−'}${movement.quantity}</span>
      <div class="history-body">
        <b>${escapeHtml(movement.product_name)}</b>
        <span><span class="type ${escapeHtml(movement.movement_type)}">${TYPE_LABELS[movement.movement_type] || escapeHtml(movement.movement_type)}</span>${who.join(' · ')}</span>
        <span>estoque ${movement.stock_before} → ${movement.stock_after}</span>
      </div>
      <time class="history-time" datetime="${escapeHtml(movement.created_at)}">${formatWhen(movement.created_at)}</time>
    </li>`;
}

function renderHistory() {
  el('history').innerHTML = state.history.length
    ? state.history.map(historyItem).join('')
    : '<li class="history-empty">Nenhuma movimentação registrada ainda.</li>';
  el('history-more').classList.toggle('hidden', !state.historyHasMore);
}

function flashCard(productId, direction) {
  const card = document.querySelector(`.card[data-id="${CSS.escape(productId)}"]`);
  if (!card) return;
  card.classList.remove('flash-in', 'flash-out');
  void card.offsetWidth;
  card.classList.add(direction === 'out' ? 'flash-out' : 'flash-in');
}

/* ------------------------------------------------------------ Diálogo ---- */

function selectedType() {
  const checked = document.querySelector('input[name="movement-type"]:checked');
  return checked ? checked.value : null;
}

function quantityValue() {
  const value = Number.parseInt(el('movement-quantity').value, 10);
  return Number.isFinite(value) ? value : 0;
}

function maxQuantity() {
  const { product, mode } = state.dialog;
  return mode === 'out' ? Math.max(product.available, 0) : 500;
}

function openMovement(productId, mode) {
  const product = state.products.find(item => item.id === productId);
  if (!product) return;
  const config = MODES[mode];
  state.dialog = { product, mode };

  el('movement-image').src = productImage(product);
  el('movement-eyebrow').textContent = config.eyebrow;
  el('movement-title').textContent = product.name;
  el('movement-stock').textContent = describeStock(product);
  el('type-field').innerHTML = config.types.map((type, index) => `
    <div class="type-option">
      <input type="radio" id="type-${type.value}" name="movement-type" value="${type.value}" ${index === 0 ? 'checked' : ''}>
      <label for="type-${type.value}">${type.title}<small>${type.hint}</small></label>
    </div>`).join('');
  // Com um único tipo possível não há o que escolher: o rádio fica marcado e oculto.
  el('type-field').classList.toggle('hidden', config.types.length === 1);

  el('movement-quantity').value = '1';
  el('movement-quantity').max = String(maxQuantity());
  el('movement-counted').value = String(product.stock);
  el('movement-buyer').value = '';
  el('movement-email').value = '';
  el('movement-note').value = '';
  el('movement-error').textContent = '';
  for (const id of ['movement-buyer', 'movement-email', 'movement-note', 'movement-counted', 'movement-quantity']) {
    el(id).removeAttribute('aria-invalid');
  }

  const submit = el('movement-submit');
  submit.querySelector('.button-label').textContent = config.submit;
  submit.classList.toggle('danger', config.danger);
  syncDialog();
  el('movement-dialog').showModal();
  setTimeout(() => (mode === 'adjust' ? el('movement-counted') : el('movement-quantity')).select(), 30);
}

function describeStock(product) {
  return `${units(product.stock)} em estoque · ${product.available} disponível(is) no marketplace`;
}

function refreshDialogProduct() {
  if (!state.dialog) return;
  const fresh = state.products.find(item => item.id === state.dialog.product.id);
  if (!fresh) return;
  state.dialog.product = fresh;
  el('movement-stock').textContent = describeStock(fresh);
  el('movement-quantity').max = String(maxQuantity());
  syncDialog();
}

function closeDialog() {
  // Enquanto o lançamento está sendo enviado, o diálogo não fecha: o
  // resultado (sucesso ou erro) precisa aparecer para o operador.
  if (!state.submitting) el('movement-dialog').close();
}

function syncDialog() {
  if (!state.dialog) return;
  const { product, mode } = state.dialog;
  const type = selectedType();
  const isSale = type === 'MANUAL_SALE';
  const isAdjust = mode === 'adjust';

  el('quantity-field').classList.toggle('hidden', isAdjust);
  el('counted-field').classList.toggle('hidden', !isAdjust);
  el('buyer-field').classList.toggle('hidden', !isSale);
  el('email-field').classList.toggle('hidden', !isSale);
  el('note-optional').textContent = isAdjust ? '(obrigatória)' : '(opcional)';
  el('movement-note').placeholder = isAdjust
    ? 'Ex.: contagem semanal, divergência encontrada…'
    : type === 'LOSS' ? 'Ex.: lata amassada, vazou…' : 'Ex.: lote recebido, pago em dinheiro…';

  let next;
  if (isAdjust) {
    next = Number.parseInt(el('movement-counted').value, 10);
    if (!Number.isFinite(next)) next = product.stock;
  } else {
    const quantity = quantityValue();
    next = mode === 'out' ? product.stock - quantity : product.stock + quantity;
  }
  const delta = next - product.stock;
  const sign = delta > 0 ? '+' : delta < 0 ? '−' : '';
  el('movement-preview').innerHTML = `Estoque de ${escapeHtml(product.name)}: <b>${product.stock} → ${next}</b> (${sign}${Math.abs(delta)})`;
  el('qty-minus').disabled = quantityValue() <= 1;
  el('qty-plus').disabled = quantityValue() >= maxQuantity();
}

function setQuantity(value) {
  const clamped = Math.min(Math.max(value, 1), Math.max(maxQuantity(), 1));
  el('movement-quantity').value = String(clamped);
  syncDialog();
}

function validateDialog() {
  const { product, mode } = state.dialog;
  const type = selectedType();
  const errors = [];
  const flag = (id, bad) => {
    if (bad) el(id).setAttribute('aria-invalid', 'true');
    else el(id).removeAttribute('aria-invalid');
    return bad;
  };

  const payload = { product_id: product.id, movement_type: type };
  if (mode === 'adjust') {
    const counted = Number.parseInt(el('movement-counted').value, 10);
    if (flag('movement-counted', !Number.isFinite(counted) || counted < 0 || counted > 10000)) errors.push('Informe a contagem física (0 a 10000).');
    else if (counted === product.stock) errors.push('A contagem é igual ao estoque atual.');
    else if (counted < product.reserved_stock) errors.push(`${product.reserved_stock} lata(s) estão reservadas em PIX; a contagem não pode ser menor.`);
    payload.counted_stock = counted;
  } else {
    const quantity = quantityValue();
    const max = maxQuantity();
    if (flag('movement-quantity', quantity < 1 || quantity > max)) {
      errors.push(max < 1 ? 'Não há latas disponíveis para saída.' : `A quantidade deve ser entre 1 e ${max}.`);
    }
    payload.quantity = quantity;
  }

  if (type === 'MANUAL_SALE') {
    const buyer = el('movement-buyer').value.trim();
    if (flag('movement-buyer', buyer.length < 2)) errors.push('Informe o nome de quem comprou.');
    payload.buyer_name = buyer;
    const email = el('movement-email').value.trim();
    if (flag('movement-email', Boolean(email) && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))) errors.push('E-mail inválido.');
    if (email) payload.buyer_email = email;
  }

  const note = el('movement-note').value.trim();
  if (flag('movement-note', mode === 'adjust' && !note)) errors.push('Explique o motivo do ajuste.');
  if (note) payload.note = note;

  return { payload, errors };
}

async function onSubmitMovement(event) {
  event.preventDefault();
  if (!state.dialog || state.submitting) return;
  const { payload, errors } = validateDialog();
  if (errors.length) {
    el('movement-error').textContent = errors.join(' ');
    return;
  }
  const button = el('movement-submit');
  state.submitting = true;
  button.disabled = true;
  button.classList.add('loading');
  el('movement-error').textContent = '';
  try {
    const result = await api('/movimentos', { method: 'POST', body: JSON.stringify(payload) });
    const index = state.products.findIndex(item => item.id === result.product.id);
    if (index >= 0) state.products[index] = result.product;
    el('movement-dialog').close();
    renderProducts();
    const { movement } = result;
    flashCard(movement.product_id, movement.delta < 0 ? 'out' : 'in');
    const buyer = movement.buyer_name ? ` para ${movement.buyer_name}` : '';
    toast(`${TYPE_LABELS[movement.movement_type]}: ${movement.delta > 0 ? '+' : '−'}${movement.quantity} ${movement.product_name}${buyer}. Estoque agora: ${movement.stock_after}.`);
    loadSummary();
    loadHistory();
  } catch (error) {
    el('movement-error').textContent = error.message;
    if (error.status === 409) {
      await loadProducts();
      refreshDialogProduct();
    }
  } finally {
    state.submitting = false;
    button.disabled = false;
    button.classList.remove('loading');
  }
}

/* ------------------------------------------------------------ Eventos ---- */

function bindEvents() {
  el('login-form').addEventListener('submit', onLogin);
  el('logout').addEventListener('click', onLogout);

  el('products').addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (!button || button.disabled) return;
    const card = button.closest('.card');
    openMovement(card.dataset.id, button.dataset.action);
  });

  el('category-filters').addEventListener('click', event => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    state.category = chip.dataset.category;
    renderCategories();
    renderProducts();
  });

  el('search').addEventListener('input', event => {
    state.search = event.target.value;
    renderProducts();
  });

  document.querySelector('.history .filters').addEventListener('click', event => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    document.querySelectorAll('.history .chip').forEach(node => node.classList.toggle('active', node === chip));
    state.historyDirection = chip.dataset.direction;
    loadHistory();
  });
  el('history-more').addEventListener('click', () => loadHistory(true));

  el('price-tables').addEventListener('submit', onSubmitPrice);
  el('price-tables').addEventListener('input', event => {
    const form = event.target.closest('.price-form');
    if (form) syncPriceForm(form);
  });

  const dialog = el('movement-dialog');
  el('movement-form').addEventListener('submit', onSubmitMovement);
  el('movement-form').addEventListener('input', event => {
    if (event.target.hasAttribute('aria-invalid')) event.target.removeAttribute('aria-invalid');
    syncDialog();
  });
  el('movement-form').addEventListener('change', syncDialog);
  dialog.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', closeDialog));
  dialog.addEventListener('cancel', event => { if (state.submitting) event.preventDefault(); });
  dialog.addEventListener('close', () => { state.dialog = null; });
  dialog.addEventListener('click', event => { if (event.target === dialog) closeDialog(); });
  el('qty-minus').addEventListener('click', () => setQuantity(quantityValue() - 1));
  el('qty-plus').addEventListener('click', () => setQuantity(quantityValue() + 1));

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.user && !dialog.open) refreshAll({ automatic: true });
  });
}

async function boot() {
  bindEvents();
  try {
    const user = await api('/me');
    showPanel(user);
  } catch {
    showLogin();
  }
}

boot();
