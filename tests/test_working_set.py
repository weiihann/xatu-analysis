from state_access import config


def test_storage_key_is_address_plus_slot():
    # A storage slot is keyed by its 20-byte address plus the 32-byte slot key.
    assert config.STORAGE_KEY_BYTES == config.ACCOUNT_KEY_BYTES + 32 == 52


def test_working_set_bytes_sums_account_and_slot_keys():
    size = config.working_set_bytes(unique_accounts=1_000, unique_storage_slots=2_000)
    assert size == 20 * 1_000 + 52 * 2_000


def test_working_set_bytes_zero():
    assert config.working_set_bytes(0, 0) == 0
