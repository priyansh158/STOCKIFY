"""SimLab: NSE simulation lab on Kite Connect data.

The plan lives in docs/simlab/ (start with README.md). Python owns data,
storage, the daemon and the runners; the C++ engine (`engine/`, imported as
`simlab._engine` through `simlab.engine`) owns orders, fills, costs and the
ledger.
"""
