-- ============================================================================
-- Rollback: Drop all tables created in migration 083
-- ============================================================================
-- Drop distributed tables first, then local tables.

DROP TABLE IF EXISTS fct_token_contract_storage_state_by_block_daily ON CLUSTER '{cluster}';
DROP TABLE IF EXISTS fct_token_contract_storage_state_by_block_daily_local ON CLUSTER '{cluster}';

DROP TABLE IF EXISTS int_token_contract_storage_state_by_block ON CLUSTER '{cluster}';
DROP TABLE IF EXISTS int_token_contract_storage_state_by_block_local ON CLUSTER '{cluster}';

DROP TABLE IF EXISTS dim_token_contract ON CLUSTER '{cluster}';
DROP TABLE IF EXISTS dim_token_contract_local ON CLUSTER '{cluster}';
