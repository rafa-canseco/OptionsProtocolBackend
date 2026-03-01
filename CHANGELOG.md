# Changelog

## [0.1.1](https://github.com/rafa-canseco/OptionsProtocolBackend/compare/v0.1.0...v0.1.1) (2026-03-01)


### Features

* add CORS middleware for localhost:3000 ([4f0c593](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/4f0c5930f8ffd044abf0fe59e4cf02be4b39dc9b))
* add MM onboarding guide for external market makers ([7ef4738](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/7ef47387a4ff7d449d015a8ed2783566f6add750))
* API endpoints, Supabase DB, circuit breaker, models ([b8c92f1](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/b8c92f1bbb30902a9cdca534fabfa41979b22ff7))
* Black-Scholes pricing model with greeks ([e2c3bdc](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/e2c3bdcc88a073617e1ea7bf8c1dd532cfef2196))
* Chainlink price feed, Deribit IV, price sheet generator ([0bdb531](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/0bdb5318202d94af31d413ae55d86776bfd6c683))
* complete test suite — 31 tests passing ([f52e55a](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/f52e55a8aa38d6a56aab60a8456a997a12f577f2))
* contracts layer — ABIs, web3 client, config for bots ([005b334](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/005b334ba785ac48b7b25fcda5bd68a4753374d1))
* update contract addresses to v5 deployment (B1N-104) ([f26912f](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/f26912f351059fbf30d1e20498574513b566c74f))


### Bug Fixes

* add Procfile for Railway start command ([8dc1b70](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/8dc1b70c168b3a24e739f1d71bc5412e53b95c03))
* correct MM expiry payout description and premium phrasing ([cfb19eb](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/cfb19eb86038a94cba43b2c53e77ef22b00790cc))
* don't cache empty otoken_map, use fresh timestamp for price cache ([2aa3125](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/2aa3125212d440aa4c54bd3b33c470cf8316053f))
* input validation, address checks, available_amount, batch count ([59478dd](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/59478dd0c2fe828676d5c096b3608c4e36bd2b5b))
* remove /api prefix — routes match PLAN.md spec ([c880855](https://github.com/rafa-canseco/OptionsProtocolBackend/commit/c88085554bef2a9538fa9142fd21414228c2b4d0))
