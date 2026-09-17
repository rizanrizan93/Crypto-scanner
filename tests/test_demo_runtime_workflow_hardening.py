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
    condition = (
        'if [ "${GITHUB_EVENT_NAME}" != "schedule" ] '
        '&& [ "${{ inputs.continuous_window }}" != "true" ]; then'
    )
    assert condition in text
    assert "total_cycles=1" in text


def test_volatility_breakout_is_rechecked_every_acquisition_cycle():
    text = Path(".github/workflows/demo-scanner-runtime.yml").read_text()
    vol = "strategy-vol-breakout-30-15-vt20-v1"
    funding = "strategy-funding-z2-oi2-v1"
    hourly_gate = 'if [ "${cycle}" -eq 1 ]; then'
    assert text.count(vol) == 1
    assert text.index(vol) < text.index(hourly_gate)
    assert text.index(funding) > text.index(hourly_gate)
