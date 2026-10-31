# Live data source notes

The existing source uses the public pump.fun frontend API v3 for real market-cap observations. Its listing endpoint returned HTTP 200 during the 2026-10-31 readiness run. The Solana Foundation public mainnet RPC returned HTTP 429 for holder queries from this environment. Missing holder risk remains unknown and blocks paper entries; it must not be converted into safe zero concentrations.

The official PublicNode Solana page lists `https://solana-rpc.publicnode.com` as its free public HTTP RPC endpoint: https://solana.publicnode.com/ . Endpoint publication does not by itself verify that every holder method is supported; perform a bounded real read-only query before selecting it.

Official Solana cluster guidance says public endpoints are rate-limited, are not intended for production applications, and can return 429 or 403. It advises dedicated/private RPC for production and honoring Retry-After: https://solana.com/docs/references/clusters . A reliable authenticated RPC URL may be required for continuously verified holder checks. Do not buy access or expose a provider credential without the user's authorized setup.
