# Toobit Crypto Pre-Move Hunter

Research-only scanner for Toobit USDT-M crypto perpetual contracts.

## Files
- `toobit_hunter.py`: scanner
- `requirements.txt`: Python dependency
- `.github/workflows/toobit-scan.yml`: scheduled GitHub Actions workflow
- `data/state.json`: created/updated automatically to remember watched candidates

## Setup
1. Upload the files preserving the exact paths shown above.
2. In GitHub repository Settings -> Secrets and variables -> Actions, create:
   - Name: `NTFY_TOPIC`
   - Secret: your ntfy topic name
3. In Settings -> Actions -> General -> Workflow permissions, select **Read and write permissions**.
4. Open Actions -> Toobit Crypto Pre-Move Hunter -> Run workflow.

The workflow is scheduled every six minutes. GitHub may delay scheduled runs.
The scanner uses public Toobit market-data endpoints and does not place orders.
It evaluates the latest 5-minute candle, which may still be forming, and stores
candidates so a later scan can confirm them before sending an ntfy alert.

This is a heuristic research tool, not a guaranteed prediction or trading advice.
