from pathlib import Path


def test_strategy_forward_evidence_workflow_is_observer_only_and_24_7():
    workflow = Path(".github/workflows/demo-strategy-forward-evidence.yml").read_text()

    assert 'cron: "*/5 * * * *"' in workflow
    assert "crypto-scanner-strategy-forward-evidence" in workflow
    assert "CRYPTO_SCANNER_TESTNET_EXECUTION: DISABLED" in workflow
    assert "BINANCE_API_KEY" not in workflow
    assert "BINANCE_API_SECRET" not in workflow
    assert "CRYPTO_SCANNER_BINANCE_API_KEY" not in workflow
    assert "CRYPTO_SCANNER_BINANCE_API_SECRET" not in workflow
    assert "crypto-scanner-cycle" not in workflow
    assert "crypto-scanner-execution-smoke" not in workflow


def test_forward_evidence_console_script_is_installed():
    config = Path("pyproject.toml").read_text()
    assert (
        'crypto-scanner-strategy-forward-evidence = '
        '"crypto_scanner.strategy_forward_evidence:main"'
    ) in config
