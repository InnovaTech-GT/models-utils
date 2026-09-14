-- resync_billing_cents.sql
-- Re-runnable RESYNC of the c1b_backfill (R2) rule set (doc 16 §2.4/§2.5/§7/§9).
--
-- Purpose: repair dual-window drift — rows written or EDITED by an old backend
-- build (floats/paid only) between the models migration and the backend
-- deploy, or during a temporary rollback to a pre-Cycle-1 build. Referenced by
-- R4 (c1d_tighten, follow-up branch feat/billing-rework/models-tighten), which
-- runs it before SET NOT NULL / CHECK tightening.
--
-- Unlike R2 (a backfill: IS NULL guards only), this script must also repair
-- rows whose cents went STALE — e.g. an old-build PATCH /orders editing the
-- float `total` after cents were already populated. Money statements therefore
-- guard with IS DISTINCT FROM the recomputed value. This is safe post-cutover:
-- the new backend dual-writes float+cents, so ledger-written rows are equal
-- and untouched. The ONE exception is order_item.unit_price_cents — a
-- point-in-time snapshot that must NOT be refreshed from the current product
-- price; it keeps the IS NULL-only guard.
--
-- Safe to run at any time, any number of times:
--   * every statement is convergent (equal rows are never touched);
--   * conversion is ROUND(x::numeric * 100)::bigint, identical to R2;
--   * statements referencing the payment ledger are gated on the table
--     existing (pre-R3 windows).
--
-- Usage: psql "$DB_URL" -f scripts/resync_billing_cents.sql
--        (wrap in a transaction if desired: psql --single-transaction)

-- §2.5.1 order.total_cents — copied from the Float, never recomputed from
-- items. IS DISTINCT FROM: repairs both NULL cents (old-build INSERT) and
-- stale cents (old-build float edit).
UPDATE "order"
SET total_cents = ROUND(total::numeric * 100)::bigint
WHERE total IS NOT NULL
  AND total_cents IS DISTINCT FROM ROUND(total::numeric * 100)::bigint;

-- §2.5.2 invoice — identity-preserving: subtotal derived so that
-- subtotal_cents + tax_cents = total_cents holds exactly.
UPDATE invoice
SET tax_cents      = ROUND(tax::numeric * 100)::bigint,
    total_cents    = ROUND(total::numeric * 100)::bigint,
    subtotal_cents = ROUND(total::numeric * 100)::bigint
                     - ROUND(tax::numeric * 100)::bigint
WHERE total IS NOT NULL AND tax IS NOT NULL
  AND (   total_cents    IS DISTINCT FROM ROUND(total::numeric * 100)::bigint
       OR tax_cents      IS DISTINCT FROM ROUND(tax::numeric * 100)::bigint
       OR subtotal_cents IS DISTINCT FROM ROUND(total::numeric * 100)::bigint
                                          - ROUND(tax::numeric * 100)::bigint);

-- §2.5.3 catalog money (NULL source stays NULL; stale cents refreshed —
-- old-build price edits write only the Float).
UPDATE product
SET price_cents = ROUND(price::numeric * 100)::bigint
WHERE price IS NOT NULL
  AND price_cents IS DISTINCT FROM ROUND(price::numeric * 100)::bigint;

UPDATE service_plan
SET price_cents = ROUND(price::numeric * 100)::bigint
WHERE price IS NOT NULL
  AND price_cents IS DISTINCT FROM ROUND(price::numeric * 100)::bigint;

UPDATE inventory_item
SET cost_cents = ROUND(cost::numeric * 100)::bigint
WHERE cost IS NOT NULL
  AND cost_cents IS DISTINCT FROM ROUND(cost::numeric * 100)::bigint;

-- §2.5.4 order_item snapshots from CURRENT product price/name — SNAPSHOT
-- semantics: fill only where missing (IS NULL), NEVER refresh an existing
-- snapshot from the live product price.
UPDATE order_item oi
SET unit_price_cents = ROUND(p.price::numeric * 100)::bigint,
    product_name     = p.name
FROM product p
WHERE oi.product_id = p.id
  AND oi.unit_price_cents IS NULL;

-- §2.5.5 payment_status / order_type coherence with the boolean/FK world.
-- NOTE: only lifts PENDING->PAID for legacy-style rows; it never demotes
-- PARTIAL/REFUNDED states written by the payment ledger.
UPDATE "order"
SET payment_status = 'PAID'
WHERE paid AND payment_status = 'PENDING';

-- Reverse direction (rollback repair): the old build's revert-payment path
-- writes paid=false but leaves payment_status='PAID'. Demote to PENDING —
-- but ONLY where no real (non-backfill) ledger payment exists; if genuine
-- ledger rows exist, the ledger is truth and PaymentService owns the status.
-- Without this, R4's CHECK (paid = (payment_status='PAID')) VALIDATE fails.
DO $$
BEGIN
    IF to_regclass('public.payment') IS NOT NULL THEN
        UPDATE "order" o
        SET payment_status = 'PENDING'
        WHERE NOT o.paid
          AND o.payment_status = 'PAID'
          AND NOT EXISTS (
              SELECT 1 FROM payment p
              WHERE p.order_id = o.id
                AND p.reference IS DISTINCT FROM 'LEGACY_BACKFILL'
                AND p.reference IS DISTINCT FROM 'RESYNC_BACKFILL'
          );
    ELSE
        -- Pre-R3 window: no ledger yet, the boolean is the only truth.
        UPDATE "order"
        SET payment_status = 'PENDING'
        WHERE NOT paid AND payment_status = 'PAID';
    END IF;
END $$;

UPDATE "order"
SET order_type = 'RECURRING'
WHERE recurring_order_id IS NOT NULL AND order_type <> 'RECURRING';

-- §2.5.6 order.client_service_id via the recurring bridge — unambiguous
-- (exactly one client_service per recurring order) mappings only.
UPDATE "order" o
SET client_service_id = b.cs_id
FROM (
    SELECT recurring_order_id AS ro_id, MIN(id::text)::uuid AS cs_id
    FROM client_service
    WHERE recurring_order_id IS NOT NULL
    GROUP BY recurring_order_id
    HAVING COUNT(*) = 1
) b
WHERE o.recurring_order_id = b.ro_id
  AND o.client_service_id IS NULL;

-- §2.5.7 payment_date for paid orders (revenue-chart date basis).
UPDATE "order" o
SET payment_date = COALESCE(
    (SELECT MAX(i.created_at) FROM invoice i
     WHERE i.order_id = o.id AND i.is_valid),
    o.created_at
)
WHERE o.paid AND o.payment_date IS NULL;

-- §2.5.8 duplicate valid invoices: keep newest, invalidate the rest.
-- (No-op once uq_invoice_order_valid exists — kept for pre-R3 windows.)
UPDATE invoice
SET is_valid = false
WHERE id IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY order_id
            ORDER BY created_at DESC, id DESC
        ) AS rn
        FROM invoice WHERE is_valid
    ) t
    WHERE rn > 1
);

-- §2.5.9 ledger synthesis for orders the OLD build marked paid during the
-- dual window / rollback (they have no ledger row; rehearsal check e5 —
-- "paid orders with total_cents>0 and no payment row" — would otherwise fail
-- and the orders would be unrefundable). Mirrors R3's LEGACY synthesis with a
-- distinct reference so backfill provenance stays auditable. Runs AFTER
-- §2.5.1/§2.5.5/§2.5.7 so amount, status, and paid_at inputs are repaired.
DO $$
BEGIN
    IF to_regclass('public.payment') IS NOT NULL THEN
        INSERT INTO payment (
            id, created_at, company_id, order_id, invoice_id, kind,
            amount_cents, method, reference, paid_at, received_by,
            reverses_payment_id, notes
        )
        SELECT gen_random_uuid(), NOW(), o.company_id, o.id,
               NULL, 'PAYMENT', o.total_cents, 'LEGACY',
               'RESYNC_BACKFILL',
               COALESCE(o.payment_date, o.created_at), NULL, NULL, NULL
        FROM "order" o
        WHERE o.paid
          AND o.total_cents > 0
          AND NOT EXISTS (SELECT 1 FROM payment p WHERE p.order_id = o.id);
    END IF;
END $$;
