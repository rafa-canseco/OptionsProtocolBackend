from scripts.replay_lazy_otoken_creations import summarize


def test_historical_mainnet_baseline_exceeds_90_percent_target() -> None:
    # Base mainnet snapshot at block 49,244,942:
    # factory length 3,042; Blockscout BatchSettler OrderExecuted range
    # 43,139,238-46,172,225 contained 170 unique oToken addresses.
    created = {f"0x{i:040x}" for i in range(3042)}
    traded = {f"0x{i:040x}" for i in range(170)}

    result = summarize(created, traded)

    assert result["eager_created"] == 3042
    assert result["unique_traded"] == 170
    assert result["creation_reduction_percent"] == 94.4116
    assert result["passes_90_percent_target"] is True


def test_replay_reports_preexisting_series_conservatively() -> None:
    result = summarize({"0x1", "0x2"}, {"0x2", "0x3"})
    assert result["created_and_traded"] == 1
    assert result["traded_preexisting_or_other_factory"] == 1
    assert result["unique_traded"] == 2
