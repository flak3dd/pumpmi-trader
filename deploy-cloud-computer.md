# Continuous worker on the selected Manus Cloud Computer

The worker host is Simran's Cloud Computer (`2yadkjq607sspggkz6ndjv2ax`). The existing managed HTTPS website remains the dashboard, reading the same durable database. The production Docker image sets `PUMPMI_WORKER_ENABLED=0`: it serves live data without creating a background worker. The VM's private runtime environment sets `PUMPMI_WORKER_ENABLED=1` and `PUMPMI_OPTIMIZER_ENABLED=1`.

## Server deployment

The source checkout is `/home/ubuntu/pumpmi-server`; isolated Python dependencies are in `/home/ubuntu/.local/share/pumpmi-venv`. `pumpmi-server.service` uses systemd, starts at boot and restarts on failure. Its API and built dashboard bind to `127.0.0.1:3001`, not the public internet. Do not open this administration port or alter the existing Manus gateway on TCP8328.

Managed database credentials are outside Git in `/home/ubuntu/.config/pumpmi/runtime.env` with mode0600. Never log or copy their contents into source, reports or chat. Preserve the existing database namespace and lease fences; do not reset bankroll, user config, watches or portfolio. Only one lease holder writes paper outcomes.

Useful commands:

```bash
sudo systemctl status pumpmi-server.service
sudo systemctl restart pumpmi-server.service
sudo journalctl -u pumpmi-server.service -n 50 --no-pager
curl http://127.0.0.1:3001/health
/home/ubuntu/.local/share/pumpmi-venv/bin/python scripts/test_deployment_roles.py
/home/ubuntu/.local/share/pumpmi-venv/bin/python scripts/test_rpc_credential.py
```

The stopped 10-minute Manus review cycle remains paused. Application-native 30-minute validation and hourly reporting run on the worker. Maximum open paper positions remains six. No wallet/signing or real-money execution is enabled.

## Protected RPC activation

The replacement endpoint is transferred and stored only as authenticated ciphertext at `/home/ubuntu/.config/pumpmi/rpc.credential.bin`. `scripts/run_cloud_worker.py` decrypts it directly into process memory using a key derived from the existing managed database credential. A tampered file or changed/missing database credential stops startup; it never falls back to an old endpoint. If database credentials rotate, reseal the RPC credential through the protected workflow. No RPC endpoint/API-key value belongs in source, commands, logs or these notes.

The initial PublicNode endpoint denied `getTokenLargestAccounts` with HTTP403 on the server. The user-supplied replacement allowed `getVersion` and `getMultipleAccounts`, but its initial holder probe returned HTTP429; probing stopped at the rate limit. This does not prove holder verification or eligible entries have been restored. Continue respecting provider backoff, curve/holder/flow/depth filters and all risk gates.

The required read-only RPC methods are `getMultipleAccounts`, `getTokenLargestAccounts`, `getTokenSupply` and `getTokenAccountsByOwner`. Never bypass denial or treat unknown risk as safe. Hosting and live prices do not establish profitable trading.

No extra Reserved hosting purchase is required for this existing VM. Its normal computer plan and traffic budget apply. For uninterrupted service if traffic exceeds the included allowance, configure the server's [traffic budget and auto top-up](https://manus.im/app#settings/usage/computer-2yadkjq607sspggkz6ndjv2ax/manage-budget).
