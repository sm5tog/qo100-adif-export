import argparse
import configparser
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pymysql
import requests

APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / 'config.ini'
SENT_LOG_PATH = APP_DIR / 'sent_qsos.json'
STATE_PATH = APP_DIR / 'state.json'

API_URL = 'https://qo100dx.club/api/qso'


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f'Saknar config.ini i {APP_DIR} (kopiera config.ini.example och fyll i).')
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding='utf-8')
    return cfg


def load_sent_ids():
    if SENT_LOG_PATH.exists():
        return set(json.loads(SENT_LOG_PATH.read_text(encoding='utf-8')))
    return set()


def save_sent_ids(ids):
    SENT_LOG_PATH.write_text(json.dumps(sorted(ids)), encoding='utf-8')


def load_last_end():
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        return datetime.fromisoformat(state['last_end'])
    return None


def save_last_end(dt):
    STATE_PATH.write_text(json.dumps({'last_end': dt.isoformat()}), encoding='utf-8')


def fetch_qsos(cfg, start_date, end_date):
    db = cfg['mariadb']
    conn = pymysql.connect(
        host=db.get('host', 'localhost'),
        port=db.getint('port', 3306),
        user=db.get('user'),
        password=db.get('password'),
        database=db.get('database', 'log4om2'),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT qsoid, stationcallsign, callsign, band, mode, qsodate, "
                "satname, propmode, gridsquare, mygridsquare "
                "FROM log WHERE satname = 'QO-100' AND qsodate >= %s AND qsodate <= %s "
                "ORDER BY qsodate",
                (start_date, end_date),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return rows


# ADIF-submoder som Log4OM lagrar direkt i 'mode'-kolumnen men som enligt
# ADIF-specen (adif.org.uk/315) egentligen är SubMode under en annan MODE.
SUBMODE_TO_MODE = {
    'FT4': 'MFSK',
    'JS8': 'MFSK',
    'FST4': 'MFSK',
    'FST4W': 'MFSK',
    'Q65': 'MFSK',
    'PSK31': 'PSK',
    'USB': 'SSB',
    'LSB': 'SSB',
    'PCW': 'CW',
    'ASCI': 'RTTY',
}


def build_record(row):
    (qsoid, station_call, call, band, mode, qsodate,
     sat_name, prop_mode, gridsquare, mygridsquare) = row
    parent_mode = SUBMODE_TO_MODE.get(mode.upper())
    record = {
        'QSO_DATE': qsodate.strftime('%Y%m%d'),
        'TIME_ON': qsodate.strftime('%H%M%S'),
        'STATION_CALLSIGN': station_call,
        'CALL': call,
        'MODE': parent_mode or mode,
        'BAND': band,
        'SAT_NAME': sat_name,
        'PROP_MODE': prop_mode,
    }
    if parent_mode:
        record['SUBMODE'] = mode
    if gridsquare:
        record['GRIDSQUARE'] = gridsquare
    if mygridsquare:
        record['MY_GRIDSQUARE'] = mygridsquare
    return qsoid, record


def write_adif(records, path):
    lines = ['ADIF export QO-100, skapat av qo100-adif-export', '<EOH>']
    for rec in records:
        parts = []
        for key, value in rec.items():
            value = str(value)
            parts.append(f'<{key}:{len(value)}>{value}')
        parts.append('<EOR>')
        lines.append(''.join(parts))
    path.write_text('\n'.join(lines), encoding='utf-8')


def upload(cfg, record):
    api_key = cfg['api']['key']
    return requests.post(
        API_URL,
        json=record,
        headers={'X-API-Key': api_key},
        timeout=15,
    )


def main():
    parser = argparse.ArgumentParser(
        description='Exportera QO-100-QSO:n från log4om2 och ladda upp till QO-100 DX Club.')
    parser.add_argument('start_date', nargs='?', default=None,
                         help='Startdatum, format ÅÅÅÅ-MM-DD. Utelämnas: fortsätter från senaste körningen.')
    parser.add_argument('--dry-run', action='store_true', help='Bygg ADIF men ladda inte upp')
    args = parser.parse_args()

    if args.start_date:
        try:
            start_date = datetime.strptime(args.start_date, '%Y-%m-%d')
        except ValueError:
            sys.exit('Ogiltigt datumformat, använd ÅÅÅÅ-MM-DD')
    else:
        start_date = load_last_end()
        if start_date is None:
            sys.exit('Inget startdatum angivet och ingen tidigare körning hittad — '
                      'ange startdatum (ÅÅÅÅ-MM-DD) första gången.')

    end_date = datetime.now()

    cfg = load_config()
    sent_ids = load_sent_ids()

    rows = fetch_qsos(cfg, start_date, end_date)
    if not rows:
        print('Inga QO-100-QSO:n hittades i intervallet.')
        if not args.dry_run:
            save_last_end(end_date)
        return

    records = []
    to_upload = []
    for row in rows:
        qsoid, record = build_record(row)
        records.append(record)
        if qsoid not in sent_ids:
            to_upload.append((qsoid, record))

    start_str = start_date.strftime('%Y%m%d')
    end_str = end_date.strftime('%Y%m%d')
    adif_path = APP_DIR / f'qo100_export_{start_str}_{end_str}.adi'
    write_adif(records, adif_path)
    print(f'ADIF-fil skriven: {adif_path} ({len(records)} QSO:n)')

    skipped = len(records) - len(to_upload)

    if args.dry_run:
        print(f'Dry-run: {len(to_upload)} nya QSO:n skulle laddas upp, {skipped} redan tidigare skickade (hoppas över).')
        return

    ok_count = 0
    fail_count = 0
    for qsoid, record in to_upload:
        try:
            resp = upload(cfg, record)
        except requests.RequestException as exc:
            print(f'  FEL {record["CALL"]} {record["QSO_DATE"]} {record["TIME_ON"]}: nätverksfel ({exc})')
            fail_count += 1
            continue

        if resp.status_code == 200:
            ok_count += 1
            sent_ids.add(qsoid)
        else:
            fail_count += 1
            try:
                errors = resp.json().get('errors')
            except ValueError:
                errors = resp.text
            print(f'  FEL {record["CALL"]} {record["QSO_DATE"]} {record["TIME_ON"]}: HTTP {resp.status_code} {errors}')
        time.sleep(0.3)

    save_sent_ids(sent_ids)
    save_last_end(end_date)

    print(f'Klart: {ok_count} skickade, {fail_count} misslyckade, {skipped} redan tidigare skickade (hoppades över).')


if __name__ == '__main__':
    main()
