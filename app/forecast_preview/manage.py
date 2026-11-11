"""Administrator cache cleanup: python -m app.forecast_preview.manage --run Q123."""

import argparse
from datetime import date

from app import db
from app.forecast_preview.service import QID


def main() -> None:
    """Delete scoped disposable previews using explicitly supplied database grants.

    Raises:
        SystemExit: Required cleanup scope or date is invalid.
        Exception: Database credentials lack DELETE or storage is unavailable.
    """
    parser = argparse.ArgumentParser(description='Remove disposable previews; lakeFS artifacts are untouched.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run', help='Run QID whose previews should be regenerated on the next visit')
    group.add_argument('--before', type=date.fromisoformat, help='Remove previews generated before YYYY-MM-DD')
    args = parser.parse_args()
    if args.run and not QID.fullmatch(args.run.upper()):
        parser.error('Use a valid run QID.')
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            if args.run:
                cur.execute('DELETE FROM model_run_preview_cache WHERE run_qid = %s', (args.run.upper(),))
            else:
                cur.execute('DELETE FROM model_run_preview_cache WHERE generated_at < %s', (args.before,))
            print(f'Removed {cur.rowcount} cached preview(s).')
        conn.commit()
    finally:
        conn.close()


if __name__ == '__main__':
    main()
