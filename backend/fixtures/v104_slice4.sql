-- Uniter v1.0.4 Slice 4 (commit a65e822) fresh database, produced by that
-- commit's own seed entry point (python -m backend.seed equivalent) and dumped
-- with sqlite3 iterdump. Schema plus seed rows only: no accounts, no secrets.
-- Used by backend/test_sim_balances.py to prove the Slice 4.1 upgrade path.
BEGIN TRANSACTION;
CREATE TABLE cash_register (
	id INTEGER NOT NULL, 
	opening_balance_cents INTEGER NOT NULL, 
	closing_balance_cents INTEGER, 
	status VARCHAR(20) NOT NULL, 
	opened_at DATETIME NOT NULL, 
	closed_at DATETIME, 
	PRIMARY KEY (id)
);
CREATE TABLE currency_denomination (
	id INTEGER NOT NULL, 
	scale_name VARCHAR(20) NOT NULL, 
	multiplier INTEGER NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (scale_name)
);
INSERT INTO "currency_denomination" VALUES(1,'post-2026',1,1,'2026-11-15 12:18:45.122215');
CREATE TABLE customers (
	id INTEGER NOT NULL, 
	phone VARCHAR(32) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	balance_cents INTEGER NOT NULL, 
	operator_code VARCHAR(50), 
	service_code VARCHAR(50), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (phone)
);
CREATE TABLE debt_ledger (
	id INTEGER NOT NULL, 
	customer_id INTEGER NOT NULL, 
	register_id INTEGER, 
	amount_cents INTEGER NOT NULL, 
	description VARCHAR(512) NOT NULL, 
	type VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT debt_ledger_type CHECK (type IN ('charge', 'payment')), 
	FOREIGN KEY(customer_id) REFERENCES customers (id), 
	FOREIGN KEY(register_id) REFERENCES cash_register (id)
);
CREATE TABLE login_attempts (
	id INTEGER NOT NULL, 
	scope VARCHAR(10) NOT NULL, 
	key_hash VARCHAR(64) NOT NULL, 
	attempted_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_login_attempts_scope CHECK (scope IN ('username', 'address'))
);
CREATE TABLE operator_balance (
	id INTEGER NOT NULL, 
	operator_id INTEGER NOT NULL, 
	balance_cents INTEGER NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (operator_id), 
	FOREIGN KEY(operator_id) REFERENCES operators (id)
);
INSERT INTO "operator_balance" VALUES(1,1,0,'2026-11-15 12:18:45.128797');
INSERT INTO "operator_balance" VALUES(2,2,0,'2026-11-15 12:18:45.136491');
CREATE TABLE operator_credit_refund (
	id INTEGER NOT NULL, 
	transaction_id INTEGER, 
	transaction_reference VARCHAR(255) NOT NULL, 
	operator_id INTEGER NOT NULL, 
	amount_cents INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT refund_amount CHECK (amount_cents >= 0), 
	UNIQUE (transaction_id), 
	FOREIGN KEY(transaction_id) REFERENCES transactions (id) ON DELETE SET NULL, 
	UNIQUE (transaction_reference), 
	FOREIGN KEY(operator_id) REFERENCES operators (id)
);
CREATE TABLE operator_topup (
	id INTEGER NOT NULL, 
	operator_id INTEGER NOT NULL, 
	amount_cents INTEGER NOT NULL, 
	reference VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(operator_id) REFERENCES operators (id), 
	UNIQUE (reference)
);
CREATE TABLE operators (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	code VARCHAR(50) NOT NULL, 
	ussd_prefix VARCHAR(255) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name), 
	UNIQUE (code)
);
INSERT INTO "operators" VALUES(1,'MTN','mtn','100',1,'2026-11-15 12:18:45.125424');
INSERT INTO "operators" VALUES(2,'Syriatel','syriatel','555',1,'2026-11-15 12:18:45.135148');
CREATE TABLE profit_ledger (
	id INTEGER NOT NULL, 
	transaction_id INTEGER NOT NULL, 
	operator_code VARCHAR(50) NOT NULL, 
	amount_cents INTEGER NOT NULL, 
	commission_cents INTEGER NOT NULL, 
	profit_cents INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (transaction_id), 
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);
CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
INSERT INTO "schema_migrations" VALUES('slice-2.3c-operator-credit-refund','2026-11-15 12:18:44');
INSERT INTO "schema_migrations" VALUES('v1.0.4-auth-foundation','2026-11-15 12:18:45');
INSERT INTO "schema_migrations" VALUES('v1.0.4-sim-cards','2026-11-15 12:18:45');
CREATE TABLE services (
	id INTEGER NOT NULL, 
	operator_id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	code VARCHAR(50) NOT NULL, 
	ussd_suffix VARCHAR(255) NOT NULL, 
	price_per_unit NUMERIC(12, 2) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_service_operator_code UNIQUE (operator_id, code), 
	FOREIGN KEY(operator_id) REFERENCES operators (id)
);
INSERT INTO "services" VALUES(1,1,'Pre-Paid Charging','prepaid','amountphone#',100,1,'2026-11-15 12:18:45.132045');
INSERT INTO "services" VALUES(2,1,'Post-Paid Bill Payment','postpaid','amountphone#',0,1,'2026-11-15 12:18:45.133915');
INSERT INTO "services" VALUES(3,2,'Pre-Paid Charging','prepaid','amountphone#',100,1,'2026-11-15 12:18:45.137843');
INSERT INTO "services" VALUES(4,2,'Post-Paid Bill Payment','postpaid','amountphone#',0,1,'2026-11-15 12:18:45.139371');
CREATE TABLE settings (
	id INTEGER NOT NULL, 
	"key" VARCHAR(255) NOT NULL, 
	value VARCHAR NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE ("key")
);
INSERT INTO "settings" VALUES(1,'commission_rate','0.05','2026-11-15 12:18:44.468921');
INSERT INTO "settings" VALUES(2,'MTN_DOMAIN','65477','2026-11-15 12:18:45.139040');
INSERT INTO "settings" VALUES(3,'SYRIATEL_DOMAIN','67858','2026-11-15 12:18:45.140921');
CREATE TABLE sim_cards (
	id INTEGER NOT NULL, 
	operator_id INTEGER NOT NULL, 
	display_name VARCHAR(80) NOT NULL, 
	phone VARCHAR(32) NOT NULL, 
	owner_user_id INTEGER, 
	is_shared BOOLEAN NOT NULL, 
	archived_at DATETIME, 
	created_by_user_id INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT sim_card_visibility CHECK ((is_shared = 1 AND owner_user_id IS NULL) OR (is_shared = 0 AND owner_user_id IS NOT NULL)), 
	CONSTRAINT sim_card_display_name CHECK (length(trim(display_name)) BETWEEN 1 AND 80), 
	FOREIGN KEY(operator_id) REFERENCES operators (id) ON DELETE RESTRICT, 
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT, 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE TABLE transactions (
	id INTEGER NOT NULL, 
	customer_id INTEGER, 
	service_id INTEGER NOT NULL, 
	amount_cents INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	ussd_string VARCHAR(255) NOT NULL, 
	reference VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	idempotency_key VARCHAR(64), 
	deleted_at DATETIME, 
	legacy_status VARCHAR(20), 
	customer_phone VARCHAR(32), 
	performed_by_user_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT transaction_status CHECK (status IN ('successful', 'failed')), 
	FOREIGN KEY(customer_id) REFERENCES customers (id), 
	FOREIGN KEY(service_id) REFERENCES services (id), 
	UNIQUE (reference), 
	UNIQUE (idempotency_key), 
	FOREIGN KEY(performed_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE TABLE user_sessions (
	id INTEGER NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	user_id INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (token_hash), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE users (
	id INTEGER NOT NULL, 
	username VARCHAR(150) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	must_change_password BOOLEAN NOT NULL, 
	password_changed_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_users_role CHECK (role IN ('admin', 'seller')), 
	UNIQUE (username)
);
CREATE INDEX ix_login_attempts_attempted_at ON login_attempts (attempted_at);
CREATE INDEX ix_login_attempts_key_hash ON login_attempts (key_hash);
CREATE INDEX ix_user_sessions_user_id ON user_sessions (user_id);
CREATE INDEX ix_sim_cards_owner_user_id ON sim_cards (owner_user_id);
CREATE INDEX ix_sim_cards_operator_id ON sim_cards (operator_id);
COMMIT;
