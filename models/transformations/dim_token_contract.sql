---
table: dim_token_contract
type: incremental
interval:
  type: block
  max: 1000
fill:
  direction: "tail"
  allow_gap_skipping: false
schedules:
  forwardfill: "@every 1m"
tags:
  - execution
  - contract
  - token
dependencies:
  - "{{external}}.canonical_execution_erc20_transfers"
  - "{{external}}.canonical_execution_erc721_transfers"
---
-- Classifies every contract that emits ERC20/ERC721 Transfer events into a single token_standard.
-- ERC20 and ERC721 Transfer share the same event signature, xatu separates them into two tables by
-- indexed-topic count, so a contract can appear in both. We assign the standard first-come-first-
-- serve: whatever its earliest Transfer event in this chunk was. The dual-standard set is tiny, so
-- the tie-break is immaterial, and this keeps each contract in exactly one mutually exclusive bucket
-- so downstream slot shares stay additive. Only not-yet-classified contracts are emitted each chunk
-- (anti-join against all existing rows), so a contract's classification is set once and kept.
INSERT INTO
  `{{ .self.database }}`.`{{ .self.table }}`
WITH
-- Contracts already classified.
existing AS (
    SELECT contract_address
    FROM `{{ .self.database }}`.`{{ .self.table }}`
),
-- Each contract's earliest transfer within this block range and its standard.
chunk_first AS (
    SELECT
        contract_address,
        argMin(token_standard, (block_number, transaction_index, log_index)) AS token_standard
    FROM (
        SELECT erc20 AS contract_address, 'erc20' AS token_standard,
               block_number, transaction_index, log_index
        FROM {{ index .dep "{{external}}" "canonical_execution_erc20_transfers" "helpers" "from" }} FINAL
        WHERE block_number BETWEEN {{ .bounds.start }} AND {{ .bounds.end }}
          AND meta_network_name = '{{ .env.NETWORK }}'
        UNION ALL
        SELECT erc721 AS contract_address, 'erc721' AS token_standard,
               block_number, transaction_index, log_index
        FROM {{ index .dep "{{external}}" "canonical_execution_erc721_transfers" "helpers" "from" }} FINAL
        WHERE block_number BETWEEN {{ .bounds.start }} AND {{ .bounds.end }}
          AND meta_network_name = '{{ .env.NETWORK }}'
    )
    GROUP BY contract_address
)
SELECT
    fromUnixTimestamp({{ .task.start }}) AS updated_date_time,
    c.contract_address AS contract_address,
    c.token_standard AS token_standard
FROM chunk_first c
GLOBAL LEFT ANTI JOIN existing e ON c.contract_address = e.contract_address
SETTINGS
    join_algorithm = 'hash',
    max_threads = 4;
