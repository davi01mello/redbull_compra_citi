from pathlib import Path


STATIC_DIR = Path(__file__).parents[1] / "app" / "static"


def test_email_screen_is_between_cart_and_pix_with_accessible_field():
    html = (STATIC_DIR / "index.html").read_text()
    assert html.index('id="shop-view"') < html.index('id="email-view"')
    assert html.index('id="email-view"') < html.index('id="payment-view"')
    assert '<label for="payer-email">E-mail</label>' in html
    assert 'type="email"' in html
    assert 'name="email"' in html
    assert 'autocomplete="email"' in html
    assert 'inputmode="email"' in html
    assert 'maxlength="254"' in html
    assert "required" in html
    assert 'id="email-error"' in html
    assert 'id="email-network-error"' in html
    assert 'id="email-privacy"' in html
    assert 'id="back-email"' in html


def test_frontend_validates_normalizes_and_prevents_duplicate_submission():
    javascript = (STATIC_DIR / "app.js").read_text()
    assert ".value.trim().toLowerCase()" in javascript
    assert "function validEmail" in javascript
    assert "state.submitting || !validEmail" in javascript
    assert "if (!validEmail(true)" in javascript
    assert "payer_email: normalizedEmail()" in javascript
    assert "show('email')" in javascript
    assert "show('payment')" in javascript
    assert "email-network-error" in javascript
    # O catch exibe o erro sem resetar o formulário, preservando o valor digitado.
    email_submit = javascript.split(
        "el('email-form').addEventListener('submit'", 1
    )[1].split("el('copy-pix')", 1)[0]
    assert "email-form').reset" not in email_submit


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
