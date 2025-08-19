-- features
CREATE UNIQUE INDEX IF NOT EXISTS idx_features_key ON features(key);

-- user_state
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_state_unique ON user_state(user_id, state_key);
CREATE INDEX IF NOT EXISTS idx_user_state_updated_at ON user_state(updated_at);

-- channel_ads
CREATE INDEX IF NOT EXISTS idx_channel_ads_status ON channel_ads(status);
CREATE INDEX IF NOT EXISTS idx_channel_ads_expire_at ON channel_ads(expire_at);
CREATE INDEX IF NOT EXISTS idx_channel_ads_last_posted_at ON channel_ads(last_posted_at);

-- notifications_outbox
CREATE INDEX IF NOT EXISTS idx_outbox_sent_at ON notifications_outbox(sent_at);
CREATE INDEX IF NOT EXISTS idx_outbox_scheduled_at ON notifications_outbox(scheduled_at);

-- pending_requests
CREATE INDEX IF NOT EXISTS idx_pending_requests_created_at ON pending_requests(created_at);
CREATE INDEX IF NOT EXISTS idx_pending_requests_status ON pending_requests(status);

-- العمليات المختلفة
CREATE INDEX IF NOT EXISTS idx_purchases_created_at ON purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_game_purchases_created_at ON game_purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_ads_purchases_created_at ON ads_purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_bill_units_created_at ON bill_and_units_purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_cash_transfer_created_at ON cash_transfer_purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_companies_transfer_created_at ON companies_transfer_purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_internet_providers_created_at ON internet_providers_purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_university_fees_created_at ON university_fees_purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_wholesale_purchases_created_at ON wholesale_purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_products_created_at ON products(created_at);

-- transactions
CREATE INDEX IF NOT EXISTS idx_transactions_timestamp ON transactions("timestamp");
CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions(created_at);
