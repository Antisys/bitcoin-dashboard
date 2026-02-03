# Bitcoin Dashboard - TODO

## High Priority (Completed)
- [x] Disk I/O monitoring - read/write speeds
- [x] Network bandwidth - download/upload speeds  
- [x] Mempool stats - size, tx count
- [x] Latest block info - height, age, tx count

## Medium Priority
- [x] Peer map/list - show connected peer details
- [x] Estimated disk needed - "Will need ~X GB more space"
- [x] Temperature monitoring - CPU/SSD temps if available
- [x] Configuration settings page - edit settings via UI

## Nice to Have
- [ ] WebSocket updates - real-time instead of polling
- [ ] Desktop notifications - alert when sync completes
- [x] Dark/light theme toggle
- [ ] Export sync log - CSV/JSON export of progress data
- [ ] Mobile app / PWA support

## Bugs / Issues
- None currently known

## Recently Completed
- 2026-02-03: Added Knots peer preference (slowly drop Core peers to favor Knots)
- 2026-02-03: Compact system stats layout (CPU larger, others combined)
- 2026-02-03: Added settings page, dark/light theme
- 2026-02-03: Rebranded to Bitcoin Knots, added Knots mempool features (Full RBF, min fee)
- 2026-02-03: Cleaned up peer display with simplified layout
- 2026-02-03: Added temperature, peer list, disk estimate
- 2026-02-03: Added download speed with blocks/s, split for AssumeUTXO
- 2026-02-03: Added disk I/O, network, mempool, latest block monitoring
- 2026-02-02: Fixed 0.0.0.0 host detection for local bitcoin-cli
