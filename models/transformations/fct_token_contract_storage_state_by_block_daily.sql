---
table: fct_token_contract_storage_state_by_block_daily
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
  - daily
  - execution
  - storage
  - contract
  - token
dependencies:
  - "{{transformation}}.int_token_contract_storage_state_by_block"
  - "{{transformation}}.int_execution_block_by_date"
---
-- Daily end-of-day live storage slots owned by ERC20/ERC721 contracts, per token_standard. The
-- network total lives in fct_contract_storage_state_by_block_daily, so the share is left to the
-- consumer (active_slots / that total). Expands to full day boundaries so partial days at the
-- incremental head get re-aggregated as more blocks arrive (ReplacingMergeTree keeps latest).
INSERT INTO `{{ .self.database }}`.`{{ .self.table }}`
WITH
    day_bounds AS (
        SELECT
            toDate(min(block_date_time)) AS min_day,
            toDate(max(block_date_time)) AS max_day
        FROM {{ index .dep "{{transformation}}" "int_execution_block_by_date" "helpers" "from" }} FINAL
        WHERE block_number BETWEEN {{ .bounds.start }} AND {{ .bounds.end }}
    ),
    blocks_in_days AS (
        SELECT
            block_number,
            block_date_time
        FROM {{ index .dep "{{transformation}}" "int_execution_block_by_date" "helpers" "from" }} FINAL
        WHERE toDate(block_date_time) >= (SELECT min_day FROM day_bounds)
          AND toDate(block_date_time) <= (SELECT max_day FROM day_bounds)
    )
SELECT
    fromUnixTimestamp({{ .task.start }}) AS updated_date_time,
    toDate(b.block_date_time) AS day_start_date,
    s.token_standard AS token_standard,
    argMax(s.active_slots, s.block_number) AS active_slots
FROM {{ index .dep "{{transformation}}" "int_token_contract_storage_state_by_block" "helpers" "from" }} AS s FINAL
GLOBAL INNER JOIN blocks_in_days AS b ON s.block_number = b.block_number
GROUP BY day_start_date, token_standard
