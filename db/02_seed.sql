-- ShipTrack orchestrator demo — sample data
-- Four shipments in different states so Postman demos have real variety.

INSERT INTO customers (name, email, phone) VALUES
    ('Alice Nguyen', 'alice@example.com', '+1-555-0101'),
    ('Bob Martinez', 'bob@example.com',   '+1-555-0102');

-- TRK100234 — Alice — cleanly in transit, on schedule
INSERT INTO shipments (tracking_number, customer_id, carrier, origin, destination, status, estimated_delivery, actual_delivery)
VALUES ('TRK100234', 1, 'FastFreight', 'Chicago, IL', 'Austin, TX', 'in_transit', CURRENT_DATE + INTERVAL '2 days', NULL);

INSERT INTO shipment_events (shipment_id, event_type, location, occurred_at, note) VALUES
    (1, 'picked_up',   'Chicago, IL',      now() - INTERVAL '2 days', 'Package picked up from sender'),
    (1, 'in_transit',  'Indianapolis, IN', now() - INTERVAL '1 day',  'Departed regional sorting facility');

-- TRK100987 — Alice — delayed due to a customs hold
INSERT INTO shipments (tracking_number, customer_id, carrier, origin, destination, status, estimated_delivery, actual_delivery)
VALUES ('TRK100987', 1, 'GlobalCargo', 'Shenzhen, CN', 'Austin, TX', 'delayed', CURRENT_DATE + INTERVAL '5 days', NULL);

INSERT INTO shipment_events (shipment_id, event_type, location, occurred_at, note) VALUES
    (2, 'picked_up',     'Shenzhen, CN',        now() - INTERVAL '9 days', 'Package picked up from sender'),
    (2, 'in_transit',    'Hong Kong',           now() - INTERVAL '7 days', 'Departed international hub'),
    (2, 'customs_hold',  'Los Angeles, CA',     now() - INTERVAL '3 days', 'Held for customs inspection — additional documentation requested'),
    (2, 'delayed',       'Los Angeles, CA',     now() - INTERVAL '1 day',  'Estimated delivery pushed back 4 days due to customs hold');

-- TRK101122 — Bob — out for delivery today
INSERT INTO shipments (tracking_number, customer_id, carrier, origin, destination, status, estimated_delivery, actual_delivery)
VALUES ('TRK101122', 2, 'FastFreight', 'Denver, CO', 'Seattle, WA', 'out_for_delivery', CURRENT_DATE, NULL);

INSERT INTO shipment_events (shipment_id, event_type, location, occurred_at, note) VALUES
    (3, 'picked_up',        'Denver, CO',   now() - INTERVAL '3 days', 'Package picked up from sender'),
    (3, 'in_transit',       'Boise, ID',    now() - INTERVAL '1 day',  'Departed regional sorting facility'),
    (3, 'out_for_delivery', 'Seattle, WA',  now() - INTERVAL '2 hours','Out for delivery, expected by end of day');

-- TRK101555 — Bob — delivered
INSERT INTO shipments (tracking_number, customer_id, carrier, origin, destination, status, estimated_delivery, actual_delivery)
VALUES ('TRK101555', 2, 'GlobalCargo', 'Miami, FL', 'Seattle, WA', 'delivered', CURRENT_DATE - INTERVAL '1 day', CURRENT_DATE - INTERVAL '1 day');

INSERT INTO shipment_events (shipment_id, event_type, location, occurred_at, note) VALUES
    (4, 'picked_up',        'Miami, FL',   now() - INTERVAL '6 days', 'Package picked up from sender'),
    (4, 'in_transit',       'Atlanta, GA', now() - INTERVAL '4 days', 'Departed regional sorting facility'),
    (4, 'out_for_delivery', 'Seattle, WA', now() - INTERVAL '1 day',  'Out for delivery'),
    (4, 'delivered',        'Seattle, WA', now() - INTERVAL '1 day',  'Delivered, signed by resident');

-- === Draft PR Agent — sample master data ====================================
-- Deliberately varied (different projects, sites, material categories) so
-- the grounded lookups (app/tools/pr_master_data_tools.py) have more than
-- one plausible match to actually disambiguate between. Includes the exact
-- job/warehouse/material/WBS values from the original submit-draft-PR
-- example payload (LE24M128 / 8322 / 6CA1M0005000000 / WP100002-S) so the
-- existing Postman flow (requests 10-18) and README examples keep working.

INSERT INTO pr_jobs (job_code, job_serial, job_name) VALUES
    ('LE24M128', 128, 'Line Expansion 24 — Phase M'),
    ('LE24M200', 200, 'Line Expansion 24 — Phase N'),
    ('RC25K010', 310, 'Refinery Capacity Upgrade — Unit 3'),
    ('PL25T045', 445, 'Pipeline Extension — Terminal 5'),
    ('WH26S002', 512, 'Warehouse Automation — Site 2'),
    ('MN24G077', 177, 'Maintenance & Turnaround — Gantry 7');

INSERT INTO pr_warehouses (warehouse_code, warehouse_name) VALUES
    ('8322', 'Main Site Warehouse'),
    ('8410', 'North Yard Warehouse'),
    ('8501', 'Central Distribution Hub'),
    ('8617', 'Coastal Staging Yard'),
    ('8730', 'East Wing Bonded Store');

-- available_qty is real stock on hand — app/store.py:decrement_material_stock()
-- reduces it every time a PR referencing that material actually submits.
INSERT INTO pr_materials (material_code, description, default_uom_code, suggested_rate, available_qty) VALUES
    ('6CA1M0005000000', 'Steel packing wire',                   'MT',  59640.00,  340),
    ('6CA1M0006000000', 'Cement OPC 53 grade',                  'BAG',   385.00, 5200),
    ('6CA1M0007000000', 'Structural steel angle 50x50x6mm',     'MT',  61200.00,  128),
    ('6CA2E0011000000', 'Copper armoured cable 3-core 25sqmm',  'MTR',   480.00, 2600),
    ('6CA2E0012000000', 'LED high-bay industrial light 150W',   'NOS',  3450.00,  210),
    ('6CA3P0021000000', 'HDPE pipe 110mm PN10',                 'MTR',   265.00, 1800),
    ('6CA3P0022000000', 'Gate valve 4 inch cast iron',          'NOS',  5200.00,   64),
    ('6CA4S0031000000', 'Safety helmet (ISI marked)',           'NOS',   220.00,  950),
    ('6CA4S0032000000', 'Fire extinguisher CO2 4.5kg',          'NOS',  2650.00,   88),
    ('6CA5C0041000000', 'Ready-mix concrete M25',                'CUM',  6800.00,  420);

-- === Vendor Agent — sample pricing =========================================
-- Several vendors per material with deliberately different prices, so
-- "cheapest vendor for X" has a real, non-trivial answer to compute (not
-- just one row). Steel gets two materials priced (Steel packing wire AND
-- Structural steel angle) since pr_materials.resolve_material's containment
-- match on the word "steel" alone can land on either one.

INSERT INTO pr_vendors (vendor_code, vendor_name) VALUES
    ('VND-1001', 'Apex Steel & Alloys'),
    ('VND-1002', 'Bharat Metal Traders'),
    ('VND-1003', 'Coastal Building Supplies'),
    ('VND-1004', 'Meridian Industrial Supply');

INSERT INTO pr_vendor_material_prices (vendor_code, material_code, price, uom_code, lead_time_days) VALUES
    -- Steel packing wire (6CA1M0005000000) — cheapest: Bharat Metal Traders
    ('VND-1001', '6CA1M0005000000', 60500.00, 'MT', 7),
    ('VND-1002', '6CA1M0005000000', 58900.00, 'MT', 10),
    ('VND-1003', '6CA1M0005000000', 61750.00, 'MT', 5),
    ('VND-1004', '6CA1M0005000000', 59200.00, 'MT', 12),
    -- Structural steel angle 50x50x6mm (6CA1M0007000000) — cheapest: Bharat Metal Traders
    ('VND-1001', '6CA1M0007000000', 62000.00, 'MT', 7),
    ('VND-1002', '6CA1M0007000000', 60400.00, 'MT', 9),
    ('VND-1004', '6CA1M0007000000', 61900.00, 'MT', 6),
    -- Cement OPC 53 grade (6CA1M0006000000) — cheapest: Coastal Building Supplies
    ('VND-1001', '6CA1M0006000000', 390.00, 'BAG', 3),
    ('VND-1003', '6CA1M0006000000', 378.00, 'BAG', 4);

INSERT INTO pr_wbs (wbs_code, job_code, description, default_cost_package_code) VALUES
    ('WP100002-S', 'LE24M128', 'Structural steel works',   '6064-2100-0'),
    ('WP100003-S', 'LE24M128', 'Civil works',               '6064-2200-0'),
    ('WP100004-S', 'LE24M128', 'Electrical works',          '6064-2300-0'),
    ('WP200001-S', 'LE24M200', 'Structural steel works',   '6064-2100-0'),
    ('WP200002-S', 'LE24M200', 'Piping works',              '6064-2400-0'),
    ('WP300001-S', 'RC25K010', 'Process unit civil works', '6065-1100-0'),
    ('WP300002-S', 'RC25K010', 'Instrumentation works',    '6065-1200-0'),
    ('WP400001-S', 'PL25T045', 'Pipeline laying works',    '6066-3100-0'),
    ('WP500001-S', 'WH26S002', 'Automation & controls',    '6067-4100-0'),
    ('WP500002-S', 'WH26S002', 'Racking & storage fit-out','6067-4200-0'),
    ('WP600001-S', 'MN24G077', 'Mechanical maintenance',   '6068-5100-0');

INSERT INTO pr_category_options (code, label) VALUES
    (0, 'Revenue'),
    (1, 'Capital');

INSERT INTO pr_planning_category_options (code, label) VALUES
    (0, 'Routine'),
    (1, 'Urgent'),
    (2, 'Planned'),
    (3, 'Emergency');

INSERT INTO pr_supply_at_options (code, label, own_premises) VALUES
    (1, 'Own premises', 'Y'),
    (2, 'Vendor',        'N');

-- Deployment-wide payload constants — same values that were previously
-- hardcoded in app/agents/pr_draft_agent.py / app/config.py, now here so
-- correcting one is an UPDATE, not a code change. See the module docstring
-- on app/tools/pr_master_data_tools.py:get_pr_constants() for what reads these.
INSERT INTO pr_constants (key, value) VALUES
    ('finyear',                            '2025-26'),
    ('comp_abb_code',                      'CMD'),
    ('dt_code',                            '30013'),
    ('ds_code',                            '41'),
    ('pr_type_code',                       '1'),
    ('detail_code',                        '27'),
    ('pr_category_code_group',             '45'),
    ('planning_category_type_code_group',  '14'),
    ('material_supply_type_code_group',    '44'),
    ('po_initiated_from',                  'CMD'),
    ('access_flag_insert',                 'I'),
    ('dopt_code_create',                   'C'),
    ('action_flag_insert',                 'I');
