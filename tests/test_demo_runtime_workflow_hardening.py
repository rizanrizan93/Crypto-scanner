from pathlib import Path


def test_demo_runtime_only_pushes_from_main():
    text = Path(".github/workflows/demo-scanner-runtime.yml").read_text()
    assert "- main" in text
    assert "fix/profit-lock-transient-read" not in text
    assert "GITHUB_REF_NAME" not in text


def test_demo_runtime_keeps_serial_safety_contract():
    text = Path(".github/workflows/demo-scanner-runtime.yml").read_text()
    assert "group: crypto-scanner-demo-runtime-scanner" in text
    assert "cancel-in-progress: false" in text
    assert "Disarmed Phase 6 pre-audit" in text
    assert "Disarmed Phase 6 post-audit" in text
    assert "crypto-scanner-profit-lock-watch ||" not in text
    assert "crypto-scanner-profit-lock-watch &" not in text


def test_non_scheduled_non_continuous_run_is_single_cycle():
    text = Path(".github/workflows/demo-scanner-runtime.yml").read_text()
    assert 'if [ "${GITHUB_EVENT_NAME}" != "schedule" ] && [ "${{ inputs.continuous_window }}" != "true" ]; then' in text
    assert "total_cycles=1" in text
