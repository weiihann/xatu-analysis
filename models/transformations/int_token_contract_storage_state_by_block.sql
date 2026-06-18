---
table: int_token_contract_storage_state_by_block
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
  - storage
  - contract
  - token
  - cumulative
dependencies:
  - "{{transformation}}.int_contract_storage_state"
  - "{{transformation}}.dim_token_contract"
---
-- Cumulative live storage slots owned by ERC20/ERC721 contracts, per block, split by token_standard.
-- Reuses the per-(block, address) slots_delta already computed in int_contract_storage_state and
-- restricts it to token contracts via dim_token_contract, summing per standard then accumulating.
-- Dense per (block, standard) so every block carries the running total forward, mirroring
-- int_contract_storage_state_by_block.
INSERT INTO
  `{{ .self.database }}`.`{{ .self.table }}`
WITH
-- Last known cumulative state per standard before this chunk. The table is dense (both standards
-- have a row almost every block) and sorted by (block_number, token_standard), so DESC + LIMIT 1 BY
-- reads only the last granule(s) - an index point-lookup, not a full-history argMax scan.
prev_state AS (
    SELECT
        token_standard,
        active_slots
    FROM `{{ .self.database }}`.`{{ .self.table }}` FINAL
    WHERE block_number < {{ .bounds.start }}
    ORDER BY block_number DESC
    LIMIT 1 BY token_standard
),
-- Per-(block, standard) slot deltas for token contracts only (sparse - only blocks with changes).
sparse_deltas AS (
    SELECT
        s.block_number AS block_number,
        t.token_standard AS token_standard,
        toInt32(SUM(s.slots_delta)) as slots_delta
    FROM {{ index .dep "{{transformation}}" "int_contract_storage_state" "helpers" "from" }} AS s FINAL
    GLOBAL INNER JOIN {{ index .dep "{{transformation}}" "dim_token_contract" "helpers" "from" }} AS t FINAL
        ON s.address = t.contract_address
    WHERE s.block_number BETWEEN {{ .bounds.start }} AND {{ .bounds.end }}
    GROUP BY s.block_number, t.token_standard
),
-- Dense grid of (block, standard) so the cumulative carries forward on every block.
standards AS (
    SELECT arrayJoin(['erc20', 'erc721']) AS token_standard
),
all_blocks AS (
    SELECT toUInt32({{ .bounds.start }} + number) as block_number
    FROM numbers(toUInt64({{ .bounds.end }} - {{ .bounds.start }} + 1))
),
block_deltas AS (
    SELECT
        b.block_number AS block_number,
        st.token_standard AS token_standard,
        COALESCE(d.slots_delta, 0) as slots_delta
    FROM all_blocks b
    CROSS JOIN standards st
    LEFT JOIN sparse_deltas d
        ON b.block_number = d.block_number AND st.token_standard = d.token_standard
)
SELECT
    fromUnixTimestamp({{ .task.start }}) as updated_date_time,
    block_number,
    block_deltas.token_standard AS token_standard,
    slots_delta,
    COALESCE(p.active_slots, 0)
        + SUM(slots_delta) OVER (PARTITION BY block_deltas.token_standard ORDER BY block_number ROWS UNBOUNDED PRECEDING) as active_slots
FROM block_deltas
GLOBAL LEFT JOIN prev_state p ON block_deltas.token_standard = p.token_standard
ORDER BY block_number, token_standard
SETTINGS
    join_algorithm = 'hash',
    max_threads = 4;
