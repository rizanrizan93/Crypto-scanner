from __future__ import annotations

import json

from crypto_scanner.persistence import (
    SCHEMA_VERSION,
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
)


def main() -> None:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("Crypto Scanner Supabase persistence is not configured")
    with SupabaseRestClient(config) as client:
        actual = client.schema_version()
    if actual != SCHEMA_VERSION:
        raise PersistenceError(
            f"Supabase schema mismatch expected={SCHEMA_VERSION} actual={actual}"
        )
    print(
        json.dumps(
            {
                "status": "PASS_PERSISTENCE_READONLY",
                "schema_version": actual,
                "backend": "SUPABASE",
                "project_isolation": "CRYPTO_SCANNER_ONLY",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
