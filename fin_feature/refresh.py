"""Atomic current-version cleaning plus conservative revision propagation."""
from fin_feature.cleaning.versioned import run_versioned_cleaning, VersionedCleanRepository
from fin_feature.features.versioned import compute_versioned, DEPENDENCIES


def refresh_current(repo, config=None):
    # Single outer transaction also owns all nested clean/compute savepoints.
    with repo.unit_of_work():
        before = {tuple(r[:3]): r[3] for r in repo.conn.execute(
            'SELECT secucode,report_date,table_type,clean_ref FROM clean_selection')}
        stats = run_versioned_cleaning(repo, config)
        adapter = VersionedCleanRepository(repo, config)
        after = {key: row['clean_id'] for key, row in adapter.results.items()}
        changed = {key[0] for key in set(before) | set(after) if before.get(key) != after.get(key)}
        # Ingestion already invalidates affected companies. Recover even when a
        # previous process stopped between clean and features, or groups were partial.
        for company, period, _ in adapter.results:
            names = {r[0] for r in repo.conn.execute(
                "SELECT feature_name FROM features WHERE secucode=? AND report_date=? ",
                (company, period))}
            if not set(DEPENDENCIES) <= names:
                changed.add(company)
        written = compute_versioned(repo, config=config, companies=changed) if changed else 0
        return dict(schema='refresh-current-v1', changed_companies=sorted(changed),
                    recomputed_features=written, clean_stats=[vars(stat) for stat in stats],
                    propagation='all saved report periods of affected companies',
                    historical_compute_logs_preserved=True)


def cmd_refresh(args):
    import json
    import sys
    import sqlite3
    from fin_feature.config import load_config
    from fin_feature.cleaning.versioned import effective_rules
    from fin_feature.cli import _get_db_and_repo
    db = None
    try:
        config = load_config(args.config)
        effective_rules(config.cleaning)
        db, repo = _get_db_and_repo(config)
        print(json.dumps(refresh_current(repo, config.cleaning), ensure_ascii=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(f'Refresh failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.close()
