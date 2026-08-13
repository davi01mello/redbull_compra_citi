from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re


STATIC_DIR = Path(__file__).parents[1] / "app" / "static"


class CheckoutStructureParser(HTMLParser):
    VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.id_counts = Counter()
        self.elements = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.id_counts[element_id] += 1
            self.elements[element_id] = {
                "attributes": attributes,
                "ancestors": tuple(self.stack),
                "tag": tag,
            }
        if tag not in self.VOID_ELEMENTS:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def parse_checkout_structure(html):
    parser = CheckoutStructureParser()
    parser.feed(html)
    return parser


def test_email_screen_is_between_cart_and_pix_with_accessible_field():
    html = (STATIC_DIR / "index.html").read_text()
    assert html.index('id="shop-view"') < html.index('id="email-view"')
    assert html.index('id="email-view"') < html.index('id="payment-view"')
    assert '<label for="payer-email">E-mail</label>' in html
    assert '<label for="buyer-name">Seu nome</label>' in html
    assert 'id="buyer-name"' in html
    assert 'id="buyer-name"' in html.split('id="email-form"', 1)[1]
    assert 'id="buyer-name"' not in html.split('id="order-form"', 1)[1].split('id="products"', 1)[0]
    assert 'type="email"' in html
    assert 'name="email"' in html
    assert 'autocomplete="email"' in html
    assert 'inputmode="email"' in html
    assert 'maxlength="254"' in html
    assert 'id="email-error"' in html
    assert 'id="email-network-error"' in html
    assert 'id="email-privacy"' in html
    assert 'id="back-email"' in html
    assert 'Informe seu email e nome' in html
    assert 'Informe seu nome e e-mail para identificarmos o pagador e processarmos corretamente o pagamento.' in html
    assert 'Usaremos estes dados somente para identificar e processar o pagamento.' in html
    assert 'Selecione seus sabores e pague rapidinho com PIX.' not in html
    assert 'pricing-summary' not in html
    assert 'Ambiente de demonstração' not in html
    assert 'Simular confirmação' not in html


def test_frontend_validates_normalizes_and_prevents_duplicate_submission():
    javascript = (STATIC_DIR / "app.js").read_text()
    assert ".value.trim().toLowerCase()" in javascript
    assert "function validEmail" in javascript
    assert "state.submitting || !hasValidName || !hasValidEmail" in javascript
    assert "!validEmail(true) || !validName(true)" in javascript
    assert "buyer_name: el('buyer-name').value.trim()" in javascript
    assert "payer_email: normalizedEmail()" in javascript
    assert "show('email')" in javascript
    assert "show('payment')" in javascript
    assert "email-network-error" in javascript
    assert '#simulate-payment' not in javascript
    assert 'pricing-summary' not in javascript
    email_submit = javascript.split(
        "el('email-form').addEventListener('submit'", 1
    )[1].split("el('copy-pix')", 1)[0]
    assert "email-form').reset" not in email_submit


def test_shop_has_fixed_checkout_bar_and_no_hidden_demo_texts():
    css = (STATIC_DIR / "styles.css").read_text()
    html = (STATIC_DIR / "index.html").read_text()
    javascript = (STATIC_DIR / "app.js").read_text()
    structure = parse_checkout_structure(html)
    checkout = structure.elements['checkout-fixed']
    checkout_rule = re.search(r"\.checkout-fixed\s*\{(?P<body>[^}]*)\}", css, re.DOTALL)
    shop_rule = re.search(r"#shop-view\s*\{(?P<body>[^}]*)\}", css, re.DOTALL)

    assert checkout_rule
    assert 'position: fixed' in checkout_rule.group('body')
    assert 'inset: auto 0 0' in checkout_rule.group('body')
    assert 'padding: 12px 16px max(12px, env(safe-area-inset-bottom))' in css
    assert 'checkout-fixed__content' in css
    assert [tag for tag, _attrs in checkout['ancestors']] == ['html', 'body']
    assert html.index('</main>') < html.index('id="checkout-fixed"') < html.index('id="toast"')
    assert structure.elements['checkout']['attributes']['form'] == 'order-form'
    for element_id in ('checkout-fixed', 'checkout', 'cart-count', 'cart-total'):
        assert structure.id_counts[element_id] == 1
    assert shop_rule
    assert 'padding-bottom: calc(var(--checkout-bar-height) + 28px)' in shop_rule.group('body')
    assert '--checkout-bar-height: 216px' in css
    assert 'simulate-payment' not in html
    assert 'simulate-payment' not in css
    assert 'demo-box' not in css
    assert 'pricing-summary' not in html
    assert "checkout.classList.toggle('hidden', view !== 'shop')" in javascript
    assert "if (view === 'shop') requestAnimationFrame(updateCheckoutHeight)" in javascript
    assert "function updateCheckoutHeight()" in javascript
    assert "checkout.getBoundingClientRect().height || checkout.offsetHeight" in javascript
    assert "window.addEventListener('resize', updateCheckoutHeight)" in javascript
    assert "new ResizeObserver" in javascript
    assert 'styles.css?v=20260813-2' in html
    assert 'app.js?v=20260813-2' in html


def test_repository_has_no_local_or_fixed_payer_fallback():
    sources = "\n".join(
        path.read_text()
        for path in [
            STATIC_DIR / "app.js",
            Path(__file__).parents[1] / "app" / "payments.py",
            Path(__file__).parents[1] / "app" / "config.py",
        ]
    )
    assert "vendas@citi.local" not in sources
    assert "MERCADOPAGO_FALLBACK_PAYER_EMAIL" not in sources
    assert "MERCADOPAGO_TEST_PAYER_EMAIL" not in sources
    assert "MERCADOPAGO_TEST_PAYER_FIRST_NAME" not in sources
