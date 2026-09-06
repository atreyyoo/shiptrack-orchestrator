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
