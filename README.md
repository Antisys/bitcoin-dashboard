# Bitcoin Sync Dashboard

A real-time web dashboard for monitoring Bitcoin Core/Knots sync progress, with full **AssumeUTXO** support.

![Dashboard Preview](https://img.shields.io/badge/Bitcoin-Dashboard-orange)

## Features

- **AssumeUTXO Support**: Shows both sync chains simultaneously
  - Background validation (0 → snapshot height)
  - Tip sync (snapshot → current tip)
- **Real-time Updates**: Auto-refreshing stats with smooth interpolation
- **System Monitoring**: CPU, memory, disk usage
- **ETA Calculation**: Estimated time to completion
- **Responsive Design**: Works on desktop and mobile

## Requirements

- Python 3.8+
- Bitcoin Core or Bitcoin Knots with RPC enabled
- `bitcoin-cli` accessible in PATH

## Installation

```bash
git clone https://github.com/Antisys/bitcoin-dashboard.git
cd bitcoin-dashboard
python3 bitcoin-dashboard.py --host localhost --port 8890
```

Then open http://localhost:8890 in your browser.

## Usage

```bash
# Local bitcoind
python3 bitcoin-dashboard.py

# Custom port
python3 bitcoin-dashboard.py --port 9000

# Remote bitcoind via SSH
python3 bitcoin-dashboard.py --host 192.168.1.100 --ssh-user ralf
```

## Configuration

The dashboard connects to bitcoind via `bitcoin-cli`. Ensure your `bitcoin.conf` has RPC enabled:

```ini
server=1
rpcuser=your_user
rpcpassword=your_password
```

## Screenshots

### AssumeUTXO Mode
Shows dual progress bars:
- Blue: Background validation (historical blocks)
- Orange: Tip sync (recent blocks)

### Normal Sync Mode
Single progress bar showing sync progress from genesis to tip.

## Bug Fixes (v1.1)

- Fixed negative block count display during interpolation
- Fixed negative speed display (clamped to 0)
- Fixed KeyError when headers not yet available
- Improved mode detection stability

## License

MIT License
