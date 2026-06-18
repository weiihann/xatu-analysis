-- ============================================================================
-- Migration 083: Token (ERC20/ERC721) contract storage share
-- ============================================================================
-- Adds:
--   1. dim_token_contract                          - classifies contracts that emit ERC20/ERC721
--                                                    Transfer events into a single token_standard.
--   2. int_token_contract_storage_state_by_block   - cumulative live storage slots owned by token
--                                                    contracts, per block, split by token_standard.
--   3. fct_token_contract_storage_state_by_block_daily - daily per-standard active_slots, the
--                                                    network total, and the share of all live slots.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. dim_token_contract
-- ----------------------------------------------------------------------------
CREATE TABLE dim_token_contract_local ON CLUSTER '{cluster}' (
    `updated_date_time` DateTime COMMENT 'Timestamp when the record was last updated' CODEC(DoubleDelta, ZSTD(1)),
    `contract_address` String COMMENT 'The token contract address' CODEC(ZSTD(1)),
    `token_standard` LowCardinality(String) COMMENT 'Mutually-exclusive classification by the standard of the first Transfer event the contract emitted (first-come-first-serve): erc20 or erc721' CODEC(ZSTD(1))
) ENGINE = ReplicatedReplacingMergeTree(
    '/clickhouse/{installation}/{cluster}/tables/{shard}/{database}/{table}',
    '{replica}',
    `updated_date_time`
) ORDER BY (contract_address)
COMMENT 'Contracts that have emitted ERC20/ERC721 Transfer events, classified into a single token_standard by their first transfer';

CREATE TABLE dim_token_contract ON CLUSTER '{cluster}' AS dim_token_contract_local ENGINE = Distributed(
    '{cluster}',
    currentDatabase(),
    dim_token_contract_local,
    cityHash64(contract_address)
);

-- ----------------------------------------------------------------------------
-- 2. int_token_contract_storage_state_by_block
-- ----------------------------------------------------------------------------
CREATE TABLE int_token_contract_storage_state_by_block_local ON CLUSTER '{cluster}' (
    `updated_date_time` DateTime COMMENT 'Timestamp when the record was last updated' CODEC(DoubleDelta, ZSTD(1)),
    `block_number` UInt32 COMMENT 'The block number' CODEC(DoubleDelta, ZSTD(1)),
    `token_standard` LowCardinality(String) COMMENT 'Token standard: erc20 or erc721' CODEC(ZSTD(1)),
    `slots_delta` Int32 COMMENT 'Change in active slots owned by contracts of this standard at this block' CODEC(DoubleDelta, ZSTD(1)),
    `active_slots` Int64 COMMENT 'Cumulative active storage slots owned by contracts of this standard at this block' CODEC(DoubleDelta, ZSTD(1))
) ENGINE = ReplicatedReplacingMergeTree(
    '/clickhouse/{installation}/{cluster}/tables/{shard}/{database}/{table}',
    '{replica}',
    `updated_date_time`
) PARTITION BY intDiv(block_number, 5000000)
ORDER BY (block_number, token_standard)
COMMENT 'Cumulative live storage slots owned by ERC20/ERC721 contracts per block, split by token_standard';

CREATE TABLE int_token_contract_storage_state_by_block ON CLUSTER '{cluster}' AS int_token_contract_storage_state_by_block_local ENGINE = Distributed(
    '{cluster}',
    currentDatabase(),
    int_token_contract_storage_state_by_block_local,
    cityHash64(block_number)
);

-- ----------------------------------------------------------------------------
-- 3. fct_token_contract_storage_state_by_block_daily
-- ----------------------------------------------------------------------------
CREATE TABLE fct_token_contract_storage_state_by_block_daily_local ON CLUSTER '{cluster}' (
    `updated_date_time` DateTime COMMENT 'Timestamp when the record was last updated' CODEC(DoubleDelta, ZSTD(1)),
    `day_start_date` Date COMMENT 'Start of the day period' CODEC(DoubleDelta, ZSTD(1)),
    `token_standard` LowCardinality(String) COMMENT 'Token standard: erc20 or erc721' CODEC(ZSTD(1)),
    `active_slots` Int64 COMMENT 'Cumulative active storage slots owned by contracts of this standard at end of day' CODEC(ZSTD(1))
) ENGINE = ReplicatedReplacingMergeTree(
    '/clickhouse/{installation}/{cluster}/tables/{shard}/{database}/{table}',
    '{replica}',
    `updated_date_time`
) PARTITION BY toYYYYMM(day_start_date)
ORDER BY (day_start_date, token_standard)
COMMENT 'Daily live storage slots owned by ERC20/ERC721 contracts, by token_standard';

CREATE TABLE fct_token_contract_storage_state_by_block_daily ON CLUSTER '{cluster}' AS fct_token_contract_storage_state_by_block_daily_local ENGINE = Distributed(
    '{cluster}',
    currentDatabase(),
    fct_token_contract_storage_state_by_block_daily_local,
    cityHash64(day_start_date)
);
