-- Somente leitura: pagamentos Mercado Pago pendentes localmente.
SELECT
    o.id AS order_id,
    o.status AS order_status,
    o.reservation_status,
    o.total AS order_amount,
    o.created_at AS order_created_at,
    o.paid_at AS order_paid_at,
    p.status AS payment_status,
    p.provider_order_id,
    p.provider_payment_id,
    p.expires_at,
    p.approved_at,
    last_event.event_id AS last_webhook_event_id,
    last_event.event_type AS last_webhook_event_type,
    last_event.processed_at AS last_webhook_processed_at
FROM orders AS o
JOIN payments AS p ON p.order_id = o.id
LEFT JOIN LATERAL (
    SELECT event_id, event_type, processed_at
    FROM webhook_events AS event
    WHERE event.provider = p.provider
      AND event.provider_order_id = p.provider_order_id
    ORDER BY event.processed_at DESC NULLS LAST, event.event_id DESC
    LIMIT 1
) AS last_event ON true
WHERE p.provider = 'MERCADOPAGO'::payment_provider
  AND p.status IN (
      'CREATING'::payment_status,
      'WAITING_PAYMENT'::payment_status
  )
ORDER BY o.created_at;

-- Somente leitura: reserved_stock armazenado versus reservas ativas.
WITH active_reservations AS (
    SELECT
        item.product_id,
        COALESCE(SUM(item.quantity), 0)::integer AS expected_reserved_stock
    FROM order_items AS item
    JOIN orders AS o ON o.id = item.order_id
    WHERE o.reservation_status = 'ACTIVE'::reservation_status
    GROUP BY item.product_id
)
SELECT
    product.id AS product_id,
    product.reserved_stock AS stored_reserved_stock,
    COALESCE(active.expected_reserved_stock, 0) AS expected_reserved_stock,
    product.reserved_stock - COALESCE(active.expected_reserved_stock, 0) AS difference
FROM products AS product
LEFT JOIN active_reservations AS active ON active.product_id = product.id
ORDER BY product.id;
