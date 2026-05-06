-- Product identity layer for grouping Privy users and verified trading wallets.

CREATE TABLE IF NOT EXISTS b1nary_accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username TEXT NOT NULL,
  username_normalized TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS b1nary_account_members (
  account_id UUID NOT NULL REFERENCES b1nary_accounts(id) ON DELETE CASCADE,
  privy_user_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner')),
  verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, privy_user_id),
  UNIQUE (privy_user_id)
);

CREATE INDEX IF NOT EXISTS idx_b1nary_account_members_account
  ON b1nary_account_members(account_id);

CREATE TABLE IF NOT EXISTS b1nary_wallets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID NOT NULL REFERENCES b1nary_accounts(id) ON DELETE CASCADE,
  privy_user_id TEXT,
  chain TEXT NOT NULL CHECK (chain IN ('base', 'solana')),
  address TEXT NOT NULL,
  address_normalized TEXT NOT NULL,
  wallet_type TEXT NOT NULL CHECK (wallet_type IN ('smart', 'embedded', 'external')),
  role TEXT NOT NULL DEFAULT 'trading' CHECK (role IN ('trading', 'funding', 'login')),
  wallet_client_type TEXT,
  verification_message TEXT,
  verification_signature TEXT,
  verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (chain, address_normalized)
);

CREATE INDEX IF NOT EXISTS idx_b1nary_wallets_account
  ON b1nary_wallets(account_id);

CREATE INDEX IF NOT EXISTS idx_b1nary_wallets_verified_trading
  ON b1nary_wallets(account_id, chain, address_normalized)
  WHERE verified_at IS NOT NULL AND role = 'trading';

CREATE TABLE IF NOT EXISTS b1nary_wallet_link_nonces (
  nonce TEXT PRIMARY KEY,
  account_id UUID NOT NULL REFERENCES b1nary_accounts(id) ON DELETE CASCADE,
  privy_user_id TEXT NOT NULL,
  chain TEXT NOT NULL CHECK (chain IN ('base', 'solana')),
  address TEXT NOT NULL,
  address_normalized TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('trading', 'funding', 'login')),
  message TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_b1nary_wallet_link_nonces_account
  ON b1nary_wallet_link_nonces(account_id);

CREATE INDEX IF NOT EXISTS idx_b1nary_wallet_link_nonces_expires
  ON b1nary_wallet_link_nonces(expires_at)
  WHERE used_at IS NULL;
