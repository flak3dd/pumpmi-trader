# Continuous worker on the selected Manus Cloud Computer

The worker host is Simran's Cloud Computer (`2yadkjq607sspggkz6ndjv2ax`). The existing managed HTTPS website remains the dashboard, reading the same durable database. The production Docker image sets `PUMPMI_WORKER_ENABLED=0`: it serves live data without creating a background worker. The VM's private runtime environment sets `PUMPMI_WORKER_ENABLED=1` and `PUMPMI_OPTIMIZER_ENABLED=1`.

## Server deployment

The source checkout is `/home/ubuntu/pumpmi-server`; isolated Python dependencies are in `/home/ubuntu/.local/share/pumpmi-venv`. `pumpmi-server.service` uses systemd, starts at boot and restarts on failure. Its API and built dashboard bind to `127.0.0.1:3001`, not the public internet. Do not open this administration port or alter the existing Manus gateway on TCP8328.

Private credentials are stored outside Git in `/home/ubuntu/.config/pumpmi/runtime.env` with mode0600. Never log or copy their contents into source, reports or chat. Use the existing managed database, namespace and lease fences; do not reset bankroll, user config, watches, or the portfolio to migrate hosts. Only one lease holder writes paper outcomes.

Useful commands:

```bash
sudo systemctl status pumpmi-server.service
sudo systemctl restart pumpmi-server.service
sudo journalctl -u pumpmi-server.service -n 50 --no-pager
curl http://127.0.0.1:3001/health
/home/ubuntu/.local/share/pumpmi-venv/bin/python scripts/test_deployment_roles.py
```

The stopped 10-minute Manus review cycle remains paused. Application-native 30-minute validation and hourly capability reporting run on the VM worker. Maximum open paper positions remains six. No wallet/signing or real-money execution is enabled.

## RPC prerequisite

The server confirms basic `getVersion` access, but the configured endpoint denies `getTokenLargestAccounts` with HTTP403. Supply an authorized read-only endpoint supporting `getMultipleAccounts`, `getTokenLargestAccounts`, `getTokenSupply`, and `getTokenAccountsByOwner`, or enable the required methods with the provider. Never bypass provider denial or treat unknown risk as zero. Hosting and live prices do not establish eligible entries or profitable results.

No extra Reserved hosting purchase is required for this existing VM. Its normal computer plan and traffic budget apply. Ensure traffic auto top-up is configured if uninterrupted service through the included traffic allowance is required.
