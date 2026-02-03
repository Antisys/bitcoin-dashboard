# Bitcoin Dashboard - TODO

## High Priority (Completed)
- [x] Disk I/O monitoring - read/write speeds
- [x] Network bandwidth - download/upload speeds  
- [x] Mempool stats - size, tx count
- [x] Latest block info - height, age, tx count

## Medium Priority
- [ ] Historical sync chart - line graph of blocks/sec over time
- [x] Peer map/list - show connected peer details
- [x] Estimated disk needed - "Will need ~X GB more space"
- [x] Temperature monitoring - CPU/SSD temps if available
- [ ] Configuration settings page - edit settings via UI

## Nice to Have
- [ ] WebSocket updates - real-time instead of polling
- [ ] Desktop notifications - alert when sync completes
- [ ] Dark/light theme toggle
- [ ] Export sync log - CSV/JSON export of progress data
- [ ] Mobile app / PWA support

## Bugs / Issues
- None currently known

## Recently Completed
- 2026-02-03: Added temperature, peer list, disk estimate
- 2026-02-03: Added download speed with blocks/s, split for AssumeUTXO
- 2026-02-03: Added disk I/O, network, mempool, latest block monitoring
- 2026-02-02: Fixed 0.0.0.0 host detection for local bitcoin-cli
