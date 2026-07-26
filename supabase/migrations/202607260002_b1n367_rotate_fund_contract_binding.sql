CREATE OR REPLACE FUNCTION v2_rotate_fund_contract_binding(
    p_chain_id BIGINT,
    p_fund_address TEXT,
    p_contract_role TEXT,
    p_expected_current_address TEXT,
    p_new_address TEXT,
    p_interface_version BIGINT,
    p_implementation_address TEXT,
    p_activation_block BIGINT
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    current_binding v2_fund_contracts%ROWTYPE;
BEGIN
    IF p_activation_block <= 1
       OR p_interface_version <= 0
       OR p_expected_current_address !~* '^0x[0-9a-f]{40}$'
       OR p_new_address !~* '^0x[0-9a-f]{40}$'
       OR lower(p_expected_current_address) = lower(p_new_address) THEN
        RAISE EXCEPTION 'Invalid versioned contract rotation';
    END IF;

    LOCK TABLE v2_fund_contracts IN SHARE ROW EXCLUSIVE MODE;

    SELECT *
    INTO current_binding
    FROM v2_fund_contracts
    WHERE chain_id = p_chain_id
      AND lower(fund_address) = lower(p_fund_address)
      AND contract_role = p_contract_role
      AND valid_to_block IS NULL
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Active contract binding not found';
    END IF;
    IF lower(current_binding.contract_address) <> lower(p_expected_current_address) THEN
        RAISE EXCEPTION 'Active contract binding differs from expected address';
    END IF;
    IF p_activation_block <= current_binding.valid_from_block THEN
        RAISE EXCEPTION 'Activation block must follow the current binding';
    END IF;

    UPDATE v2_fund_contracts
    SET valid_to_block = p_activation_block - 1
    WHERE chain_id = current_binding.chain_id
      AND lower(fund_address) = lower(current_binding.fund_address)
      AND lower(contract_address) = lower(current_binding.contract_address)
      AND valid_from_block = current_binding.valid_from_block;

    INSERT INTO v2_fund_contracts (
        chain_id,
        fund_address,
        contract_address,
        contract_role,
        interface_version,
        implementation_address,
        valid_from_block,
        valid_to_block
    ) VALUES (
        p_chain_id,
        current_binding.fund_address,
        lower(p_new_address),
        p_contract_role,
        p_interface_version,
        NULLIF(lower(p_implementation_address), ''),
        p_activation_block,
        NULL
    );
END;
$$;

REVOKE ALL ON FUNCTION v2_rotate_fund_contract_binding(
    BIGINT, TEXT, TEXT, TEXT, TEXT, BIGINT, TEXT, BIGINT
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION v2_rotate_fund_contract_binding(
    BIGINT, TEXT, TEXT, TEXT, TEXT, BIGINT, TEXT, BIGINT
) TO service_role;
