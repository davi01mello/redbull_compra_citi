const pricingAudienceByUrl = { member: 'MEMBER', 'non-member': 'NON_MEMBER' };
const pricingParam = new URLSearchParams(window.location.search).get('pricing');
const state = {
  products: [],
  pricingTiers: [],
  pricingAudience: pricingAudienceByUrl[pricingParam] || null,
  cart: {},
  order: null,
  loading: true,
  submitting: false
};
const money = value => new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(value);
const el = id => document.getElementById(id);
const escapeHtml = value => String(value).replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);

let toastTimer;
function toast(message) {
  clearTimeout(toastTimer);
  el('toast').textContent = message;
  el('toast').classList.add('show');
  toastTimer = setTimeout(() => el('toast').classList.remove('show'), 3200);
}

async function api(url, options = {}) {
  const response = await fetch(url, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || 'Não foi possível concluir a operação. Tente novamente.');
  }
  return response.json();
}

function productClass(product) {
  if (product.id === 'tropical') return 'tropical';
  if (product.id === 'melancia') return 'melancia';
  return 'original';
}

function productImage(product) {
  const images = {
    tropical: '/static/assets/red-bull-tropical-transparent.png?v=20260802-2',
    melancia: '/static/assets/red-bull-melancia-transparent.png?v=20260802-2',
    original: '/static/assets/red-bull-original-transparent.png?v=20260802-2'
  };
  return images[product.id] || images.original;
}

function priceTierForQuantity(quantity) {
  return state.pricingTiers.find(tier => (
    quantity >= tier.min_quantity
    && (tier.max_quantity === null || quantity <= tier.max_quantity)
  ));
}

function tierQuantityLabel(tier) {
  if (tier.max_quantity === null && tier.min_quantity === 1) return 'qualquer quantidade';
  if (tier.max_quantity === null) return `${tier.min_quantity}+ unidades`;
  if (tier.min_quantity === tier.max_quantity) return `${tier.min_quantity} unidade`;
  return `${tier.min_quantity}–${tier.max_quantity} unidades`;
}

function renderPricingSummary() {
  const audience = state.pricingAudience === 'MEMBER' ? 'Preço para membros' : 'Preço para não membros';
  const tiers = state.pricingTiers.map(tier => `${tierQuantityLabel(tier)}: ${money(tier.unit_price)}`).join(' · ');
  el('pricing-summary').textContent = `${audience} — ${tiers}`;
}

async function loadProducts() {
  state.loading = true;
  el('products').setAttribute('aria-busy', 'true');
  try {
    if (!state.pricingAudience) {
      throw new Error('Link de compra inválido. Escaneie novamente o QR Code do ponto de venda.');
    }
    [state.products, state.pricingTiers] = await Promise.all([
      api('/api/products'),
      api(`/api/price-tiers?pricing_audience=${encodeURIComponent(state.pricingAudience)}`)
    ]);
    if (!state.pricingTiers.length) {
      throw new Error('Não há uma faixa de preço disponível para este ponto de venda.');
    }
    renderPricingSummary();
    const order = { tropical: 0, melancia: 1, original: 2 };
    state.products.sort((a, b) => (order[a.id] ?? 99) - (order[b.id] ?? 99));
    const validIds = new Set(state.products.map(product => product.id));
    Object.keys(state.cart).forEach(id => { if (!validIds.has(id)) delete state.cart[id]; });
    renderProducts();
  } finally {
    state.loading = false;
    el('products').setAttribute('aria-busy', 'false');
    validateForm();
  }
}

function renderProducts() {
  if (!state.products.length) {
    el('products').innerHTML = '<div class="product sold-out"><div class="product-copy"><h3>Nenhum produto disponível</h3><p>Tente novamente em instantes.</p></div></div>';
    el('stock-note').textContent = 'Indisponível';
    return;
  }

  const available = state.products.filter(product => product.stock > 0).length;
  const selectedCount = Object.values(state.cart).reduce((sum, quantity) => sum + quantity, 0);
  const currentTier = priceTierForQuantity(selectedCount || 1);
  const unitPriceLabel = currentTier
    ? `${money(currentTier.unit_price)} por unidade`
    : 'Preço indisponível';
  el('stock-note').textContent = `${available} ${available === 1 ? 'sabor disponível' : 'sabores disponíveis'}`;
  el('products').innerHTML = state.products.map(product => {
    const quantity = state.cart[product.id] || 0;
    const soldOut = product.stock === 0;
    const safeName = escapeHtml(product.name.replace('Energy ', ''));
    return `
      <article class="product ${quantity ? 'selected' : ''} ${soldOut ? 'sold-out' : ''}">
        <div class="product-image ${productClass(product)}">
          <img src="${productImage(product)}" alt="Lata de Red Bull ${safeName}">
        </div>
        <div class="product-copy">
          <h3>${safeName}</h3>
          <p class="${soldOut ? 'stock-label' : ''}">${soldOut ? 'Esgotado' : unitPriceLabel}</p>
        </div>
        <div class="quantity" aria-label="Quantidade de ${safeName}">
          <button type="button" data-id="${escapeHtml(product.id)}" data-delta="-1" aria-label="Diminuir ${safeName}" ${quantity === 0 ? 'disabled' : ''}>−</button>
          <b aria-live="polite">${quantity}</b>
          <button type="button" data-id="${escapeHtml(product.id)}" data-delta="1" aria-label="Aumentar ${safeName}" ${soldOut || quantity >= product.stock ? 'disabled' : ''}>+</button>
        </div>
      </article>`;
  }).join('');

  document.querySelectorAll('[data-delta]').forEach(button => {
    button.addEventListener('click', () => changeQuantity(button.dataset.id, Number(button.dataset.delta)));
  });
  renderCart();
}

function changeQuantity(id, delta) {
  const product = state.products.find(item => item.id === id);
  if (!product) return;
  state.cart[id] = Math.max(0, Math.min(product.stock, (state.cart[id] || 0) + delta));
  renderProducts();
}

function cartDetails() {
  const entries = Object.entries(state.cart).filter(([, quantity]) => quantity > 0);
  const count = entries.reduce((sum, [, quantity]) => sum + quantity, 0);
  const tier = count ? priceTierForQuantity(count) : null;
  const total = tier ? count * tier.unit_price : 0;
  return { entries, count, total, tier };
}

function renderCart() {
  const { count, total } = cartDetails();
  el('cart-count').textContent = `${count} ${count === 1 ? 'item' : 'itens'}`;
  el('cart-total').textContent = money(total);
  validateForm();
}

function validName(showError = false) {
  const input = el('buyer-name');
  const value = input.value.trim();
  const valid = value.length >= 2;
  const shouldShow = showError && !valid;
  input.closest('.name-field').classList.toggle('invalid', shouldShow);
  input.setAttribute('aria-invalid', String(shouldShow));
  el('name-error').textContent = shouldShow ? 'Digite um nome com pelo menos 2 caracteres.' : '';
  return valid;
}

function validateForm(showNameError = false) {
  const cart = cartDetails();
  const hasPricedItems = cart.count > 0 && Boolean(cart.tier);
  el('checkout').disabled = state.loading || state.submitting || !hasPricedItems || !validName(showNameError);
}

function setSubmitting(value) {
  state.submitting = value;
  el('checkout').classList.toggle('loading', value);
  el('checkout').setAttribute('aria-busy', String(value));
  validateForm();
}

function show(view) {
  document.querySelectorAll('.view').forEach(item => item.classList.add('hidden'));
  el(`${view}-view`).classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderSuccess() {
  if (!state.order) return;
  const count = state.order.items.reduce((sum, item) => sum + item.quantity, 0);
  el('success-order').textContent = `Pedido #${state.order.id.slice(0, 8).toUpperCase()}`;
  el('success-count').textContent = `${count} ${count === 1 ? 'item' : 'itens'}`;
  el('success-total').textContent = money(state.order.total);
  el('success-items').innerHTML = state.order.items.map(item => `${item.quantity}× ${escapeHtml(item.product_name.replace('Energy ', ''))}`).join('<br>');
}

el('buyer-name').addEventListener('input', () => validateForm(false));
el('buyer-name').addEventListener('blur', () => validName(true));

el('order-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!validName(true) || !cartDetails().count || state.submitting) return;
  setSubmitting(true);
  try {
    const items = cartDetails().entries.map(([product_id, quantity]) => ({ product_id, quantity }));
    state.order = await api('/api/orders', {
      method: 'POST',
      body: JSON.stringify({
        buyer_name: el('buyer-name').value.trim(),
        pricing_audience: state.pricingAudience,
        items
      })
    });
    el('qr-code').src = state.order.payment.qr_code;
    el('pix-code').textContent = state.order.payment.copy_paste;
    el('payment-total').textContent = money(state.order.total);
    show('payment');
  } catch (error) {
    toast(error.message);
    await loadProducts().catch(() => {});
  } finally {
    setSubmitting(false);
  }
});

el('copy-pix').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(el('pix-code').textContent);
    el('copy-pix').querySelector('span').textContent = 'Código copiado!';
    toast('Código PIX copiado.');
    setTimeout(() => { el('copy-pix').querySelector('span').textContent = 'Copiar código PIX'; }, 2000);
  } catch {
    toast('Não foi possível copiar. Selecione o código manualmente.');
  }
});

el('simulate-payment').addEventListener('click', async () => {
  if (!state.order) return;
  const button = el('simulate-payment');
  try {
    button.disabled = true;
    button.textContent = 'Confirmando...';
    await api(`/api/mock/pay/${state.order.payment.txid}`, { method: 'POST' });
    renderSuccess();
    show('success');
  } catch (error) {
    toast(error.message);
    button.disabled = false;
    button.textContent = 'Simular confirmação';
  }
});

el('back-shop').addEventListener('click', () => show('shop'));
el('new-order').addEventListener('click', async () => {
  state.cart = {};
  state.order = null;
  el('order-form').reset();
  el('simulate-payment').disabled = false;
  el('simulate-payment').textContent = 'Simular confirmação';
  await loadProducts().catch(error => toast(error.message));
  location.hash = '#/comprar';
  show('shop');
});

function route() {
  const activeProtectedView = ['payment', 'success'].some(view => !el(`${view}-view`).classList.contains('hidden'));
  if (!activeProtectedView) show('shop');
}

window.addEventListener('hashchange', route);
loadProducts().then(route).catch(error => {
  state.loading = false;
  el('products').setAttribute('aria-busy', 'false');
  el('products').innerHTML = '<div class="product sold-out"><div class="product-copy"><h3>Não foi possível carregar</h3><p>Atualize a página para tentar novamente.</p></div></div>';
  el('stock-note').textContent = 'Erro ao carregar';
  el('pricing-summary').textContent = 'Preço indisponível para este link.';
  toast(error.message);
});
