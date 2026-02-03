# Bitcoin Dashboard Project

## Overview
Bitcoin sync monitoring dashboard with real-time stats display.

## Key Files
- `bitcoin-dashboard.py` - Main dashboard application (Python HTTP server)
- `TODO.md` - Feature roadmap and task tracking

## Architecture
- Single-file Python web server with embedded HTML/CSS/JS
- Polls Bitcoin Core RPC for blockchain stats
- Polls /proc for system stats (CPU, memory, disk I/O, network, temp)
- Supports AssumeUTXO dual-sync mode visualization

## Bitcoin Setup on This Machine
- Bitcoin Knots v28.1 installed at `/usr/local/bin/bitcoind`
- Config: `~/.bitcoin/bitcoin.conf`
- Chainstate on NVMe (fast), blocks on external 1TB SSD (`/media/ralf/external/bitcoin/blocks`)
- RPC: localhost:8332, user=bitcoin, pass=bitcoinrpc
- Systemd services: `bitcoind.service`, `bitcoin-dashboard.service`

## Dashboard Features
- Sync progress with AssumeUTXO support (dual progress bars)
- Download speed (MB/s + blocks/s)
- Disk I/O, Network I/O
- CPU per-core usage, Memory, Temperature (CPU + NVMe)
- Mempool stats
- Connected peers list
- Estimated disk space needed

## GitHub
- Repo: https://github.com/Antisys/bitcoin-dashboard
- Push from 192.168.1.145 (has GitHub auth configured)

## Network Machines
- 192.168.1.152 - This machine (Bitcoin node + dashboard)
- 192.168.1.145 - Has GitHub auth, bitcoin-dashboard repo
- 192.168.1.96 - Has UTXO snapshots
- 192.168.1.111 - Banana Pi (future Bitcoin node)

## Common Commands
```bash
# Dashboard
sudo systemctl restart bitcoin-dashboard
curl http://localhost:8890/api/stats | jq

# Bitcoin
bitcoin-cli getblockchaininfo
bitcoin-cli getchainstates  # For AssumeUTXO status
sudo systemctl status bitcoind
```
