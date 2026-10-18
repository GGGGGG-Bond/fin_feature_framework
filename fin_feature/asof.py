"""Read-only, bounded history queries over actually saved statement observations."""
import json
from collections import defaultdict
from datetime import date, datetime, time, timezone

from fin_feature.cleaning.dedup import sort_key
from fin_feature.cleaning.versioned import (
    VersionedCleanRepository, validate_versions, effective_rules, _prepare, digest,
)
from fin_feature.storage.hashing import canonical_json
from fin_feature.utils.date_utils import normalize_date


def utc_timestamp(value):
    if not isinstance(value, str):
        raise ValueError('an ISO timestamp with explicit timezone is required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp timezone is unknown')
    return parsed.astimezone(timezone.utc)


def cutoff(value, view):
    if view not in ('system', 'public'):
        raise ValueError('view must be system or public')
    if not isinstance(value, str):
        raise ValueError('as_of must be an ISO date/timestamp')
    if len(value) == 10:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError('normalized ISO date required')
        return day if view == 'public' else datetime.combine(day, time.max, timezone.utc)
    if view == 'public':
        raise ValueError('public view uses a disclosure date at end of day, not intraday timestamps')
    return utc_timestamp(value)


def _public_date(observation):
    kind = observation['revision_type']
    if kind == 'initial':
        value = observation['notice_date']
    elif kind in ('revision', 'error_correction'):
        value = observation['revision_notice_date']
    else:
        return None
    # Do not silently truncate arbitrary strings, infer publication from fetching,
    # or use the original notice for a later revision.
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def _selection(version, observation):
    raw = json.loads(version['raw_fields'] or '{}')
    raw['report_date'] = normalize_date(version['report_date'])
    raw['notice_date'] = normalize_date(observation['notice_date'])
    raw['entry_time'] = observation['entry_time']
    return sort_key(raw)


def select_asof(repo, as_of, view='system', secucode=None, source_name='eastmoney'):
    """Return saved versions plus coverage diagnostics; never use current flags.

    Public dates are explicit caller/source declarations, not automatic verification
    of official publication. Production unknown revision types are deliberately
    excluded. System time comes from timezone-aware snapshot receipt observations.
    """
    boundary = cutoff(as_of, view)
    sql = 'SELECT * FROM statement_version WHERE source_name=?'
    args = [source_name]
    if secucode is not None:
        sql += ' AND secucode=?'; args.append(secucode)
    grouped = defaultdict(list)
    for row in repo.conn.execute(sql, args):
        version = dict(row)
        grouped[(row['secucode'], row['report_date'], row['table_type'])].append(version)
    selected, diagnostics = [], []
    for key, versions in sorted(grouped.items()):
        candidates = []
        reasons = set()
        for version in versions:
            observations = repo.conn.execute(
                'SELECT o.*, s.fetched_at AS snapshot_fetched_at FROM statement_observation o '
                'JOIN raw_snapshot s ON s.snapshot_id=o.snapshot_id WHERE o.version_id=?',
                (version['version_id'],)).fetchall()
            if not observations:
                reasons.add('missing_trusted_observation')
            for observation in observations:
                try:
                    received = utc_timestamp(observation['observed_at'])
                    if received != utc_timestamp(observation['snapshot_fetched_at']):
                        raise ValueError('observation differs from actual receipt time')
                except (ValueError, TypeError):
                    reasons.add('unknown_observation_time'); continue
                if view == 'system':
                    if received > boundary:
                        continue
                    rank = _selection(version, observation)
                else:
                    public = _public_date(observation)
                    if public is None:
                        reasons.add('unknown_public_time'); continue
                    if public > boundary:
                        continue
                    # Metadata is asserted for this exact observation/version.
                    # It can reconstruct public history from a later observation,
                    # but does NOT claim that this system knew it then.
                    rank = (public.isoformat(), *_selection(version, observation)[1:])
                candidates.append((rank, version['version_id'], version, dict(observation)))
        detail = dict(secucode=key[0], report_date=key[1], table_type=key[2])
        if candidates:
            candidates.sort(key=lambda c: (c[0], c[1], c[3]['observation_id']))
            rank, _, version, observation = candidates[-1]
            version = dict(version)
            version['selected_observation_id'] = observation['observation_id']
            version['selected_observed_at'] = observation['observed_at']
            version['selected_notice_date'] = observation['notice_date']
            version['selected_normalization_diagnostics'] = observation['normalization_diagnostics']
            public = _public_date(observation)
            version['selected_public_date'] = public.isoformat() if public else None
            selected.append(version)
            contenders = {c[1] for c in candidates if c[0][:2] == rank[:2]}
            if len(contenders) > 1:
                reasons.add('ambiguous_same_date_selection')
        else:
            reasons.add('no_saved_eligible_version')
        for reason in sorted(reasons):
            diagnostics.append({**detail, 'reason_code': reason})
    validate_versions(repo, selected)
    for version in selected:
        version['normalization_diagnostics'] = version['selected_normalization_diagnostics']
    return selected, diagnostics


class AsOfInputs(VersionedCleanRepository):
    """Pure cleaning of selected saved versions; no current projection writes."""
    def __init__(self, repo, versions, config=None):
        self.config = effective_rules(config)
        self.config_hash = digest(self.config)
        self.records, self.results, self.results_by_id = {}, {}, {}
        for original in versions:
            version = {**original, 'notice_date': original['selected_notice_date']}
            vals, compat, diagnostics, status = _prepare(version, self.config)
            clean_id = digest({'record_ref': version['version_id'], 'observation_id': version['selected_observation_id'],
                               'config_hash': self.config_hash, 'scope': 'asof-virtual'})
            key = (version['secucode'], version['report_date'], version['table_type'])
            clean = dict(clean_id=clean_id, record_ref=version['version_id'], status=status,
                         diagnostics=canonical_json(diagnostics), version=version)
            self.results[key] = self.results_by_id[clean_id] = clean
            if status == 'kept':
                self.records[key] = {**compat, **vals, 'record_ref': version['version_id'],
                                     'clean_ref': clean_id, 'industry_type': version['industry_type'],
                                     '__standardized__': True, '__diagnostics__': diagnostics}
                from fin_feature.features.reviewed import comparison_bases
                self.records[key]['__comparison_bases__'] = comparison_bases(version, self.config)


def query_asof(repo, as_of, view='system', secucode=None, config=None, include_features=False):
    """Consistent read-only snapshot. Result is an export, not a persisted compute run."""
    from fin_feature.features.versioned import evaluate_feature, DEPENDENCIES, code_identity
    owns = not repo.conn.in_transaction
    if owns:
        repo.conn.execute('BEGIN')
    try:
        versions, diagnostics = select_asof(repo, as_of, view, secucode)
        results = []
        if include_features:
            adapter = AsOfInputs(repo, versions, config)
            pairs = sorted({key[:2] for key in adapter.results})
            for company, period in pairs:
                for name in DEPENDENCIES:
                    calculated = evaluate_feature(adapter, company, period, name)
                    results.append(dict(secucode=company, report_date=period, feature_name=name, **calculated))
        return dict(schema='asof-query-v1', as_of=as_of, view=view,
                    boundary='UTC end-of-day or explicit timestamp' if view == 'system' else 'declared public date end-of-day',
                    statements=versions, selected_count=len(versions), diagnostics=diagnostics,
                    features=results, code_identity=code_identity(), current_projections_written=False,
                    clean_ref_scope='virtual/in-memory; not persisted replay records',
                    complete_market_history_verified=False,
                    limitations=['Only actually saved versions/observations can be selected; missing older history cannot be restored.',
                                 'Public dates require explicit initial/revision declarations; unknowns are excluded.',
                                 'Date granularity does not establish intraday availability; public view is not system knowledge.',
                                 'Unknown financial comparability still blocks cross-period feature values.'])
    finally:
        if owns:
            repo.conn.rollback()


def cmd_asof(args):
    import sys
    import sqlite3
    from fin_feature.config import load_config
    from fin_feature.storage.database import get_readonly_connection
    from fin_feature.storage.repository import Repository
    conn = None
    try:
        cutoff(args.as_of, args.view)
        config = load_config(args.config)
        conn = get_readonly_connection(config.db_path)
        report = query_asof(Repository(conn), args.as_of, args.view, args.secucode,
                            config.cleaning, args.features)
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return 0 if report['selected_count'] and not report['diagnostics'] else 1
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(f'As-of query failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()
