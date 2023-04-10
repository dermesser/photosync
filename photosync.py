#!/usr/bin/env python3

import datetime
import json
import os
import os.path
import pickle
import sqlite3

import arguments
import dateutil.parser
import httplib2

from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

PROD = False
TRACE = True


def log(level, msg, *args):
    if PROD:
        return
    if level == 'TRACE' and not TRACE:
        return
    if args:
        msg = msg.format(*args)
    print (level, "::", msg)

def make_date_iso(d):
    """Expects a date like 2019-1-4 and preprocesses it for ISO parsing.
    """
    return '-'.join('{:02d}'.format(int(p)) for p in d.split('-'))

class TokenSource:
    """Return OAuth token for PhotosService to use.

    Please acquire your own client secret and put it into the clientsecret.json
    (or any other file) in the directory you are running this program from.

    On first use, this will prompt you for authorization on stdin. On subsequent
    invocations, it will use the token from the database and refresh it when
    needed.
    """
    SCOPES = ['https://www.googleapis.com/auth/photoslibrary']
    CRED_ID = 'installed.main'

    def __init__(self, db=None, tokensfile=None, clientsecret=None):
        self._db = db
        self._tokensfile = tokensfile
        self._clientsecret = clientsecret

    def creds(self):
        if self._clientsecret is None:
            if self._tokensfile and os.path.exists(self._tokensfile):
                with open(self._tokensfile, 'rb') as f:
                    creds = pickle.load(f)
                    return creds
            elif self._db:
                creds = self._db.get_credentials(self.CRED_ID)
                if creds:
                    creds = pickle.loads(creds)
                    return creds
        assert self._clientsecret is not None, 'Need --creds to proceed with authorization'
        flow = InstalledAppFlow.from_client_secrets_file(self._clientsecret, self.SCOPES)
        creds = flow.run_local_server()
        if creds and self._tokensfile:
            with open(self._tokensfile, 'wb') as f:
                pickle.dump(creds, f)
        if creds and self._db:
            self._db.store_credentials(self.CRED_ID, pickle.dumps(creds))
        return creds


class PhotosService:

    def __init__(self, tokens=None):
        self._token_source = tokens
        self._service = build('photoslibrary', 'v1', credentials=tokens.creds())
        self._http = httplib2.Http()

    def get_item(self, id):
        item = self._service.mediaItems().get(mediaItemId=id).execute()
        return item

    def list_library(self, start=None, to=None):
        """Yields items from the library.

        Arguments:
            start: datetime.date
            end: datetime.date
            
        Returns:
            [mediaItem]
        """
        filters = {}
        if start or to:
            rng_filter = {'ranges': {}}
            if not start:
                start = datetime.date(1999, 1, 1)
            if not to:
                to = datetime.datetime.now().date()
            rng_filter['ranges']['startDate'] = {'year': start.year, 'month': start.month, 'day': start.day}
            rng_filter['ranges']['endDate'] = {'year': to.year, 'month': to.month, 'day': to.day}
            filters['dateFilter'] = rng_filter
        pagetoken = None

        # Photos are returned in reversed order of creationTime.
        while True:
            resp = self._service.mediaItems().search(body={'pageSize': 75, 'filters': filters, 'pageToken': pagetoken}).execute()
            pagetoken = resp.get('nextPageToken', None)
            items = resp.get('mediaItems', None)
            if not items:
                return
            for i in items:
                log('TRACE', i['mediaMetadata']['creationTime'])
                yield i
            if pagetoken is None:
                return

    def download_items(self, items):
        """Download multiple items.

        Arguments:
            items: List of (id, path, video) tuples.

        Returns:
            List of IDs that were successfully downloaded.
        """
        ids = [i[0] for i in items]
        media_items = self._service.mediaItems().batchGet(mediaItemIds=ids).execute()
        ok = []
        i = -1
        for result in media_items['mediaItemResults']:
            i += 1
            if 'status' in result:
                log('WARN', 'Could not query info for {}: {}'.format(items[i][0], result['status']))
                continue
            item = result['mediaItem']
            rawurl = item['baseUrl']
            if 'video' in item['mediaMetadata']:
                rawurl += '=dv'
            else:
                rawurl += '=d'
            os.makedirs(items[i][1], exist_ok=True)
            p = os.path.join(items[i][1], item['filename'])
            log('INFO', 'Downloading {}', p)
            resp, cont = self._http.request(rawurl, 'GET')
            if resp.status != 200:
                log('WARN', 'HTTP item download failed: {} {}'.format(resp.status, resp.reason))
                continue
            with open(p, 'wb') as f:
                f.write(cont)
            size = len(cont) / (1024. * 1024.)
            log('INFO', 'Downloaded {} successfully ({:.2f} MiB)', p, size)
            ok.append(item['id'])
        return ok


class DB:

    def __init__(self, path):
        self._db = sqlite3.connect(path)
        self.initdb()
        self._dtparse = dateutil.parser.isoparser()

    def initdb(self):
        cur = self._db.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, creationTime TEXT, path TEXT, mimetype \
                TEXT, filename TEXT, video INTEGER, offline INTEGER)')
        cur.execute('CREATE TABLE IF NOT EXISTS transactions (id TEXT, type TEXT, time INTEGER)')
        cur.execute('CREATE TABLE IF NOT EXISTS oauth (id TEXT PRIMARY KEY, credentials BLOB)')
        self._db.commit()

    def store_credentials(self, id, creds):
        with self._db as conn:
            cur = conn.cursor()
            cur.execute('SELECT id FROM oauth WHERE id = ?', (id,))
            if not cur.fetchone():
                cur.execute('INSERT INTO oauth (id, credentials) VALUES (?, ?)', (id, creds))
                return
            cur.close()
            cur = conn.cursor()
            cur.execute('UPDATE oauth SET credentials = ? WHERE id = ?', (creds, id))

    def get_credentials(self, id):
        with self._db as conn:
            cur = conn.cursor()
            cur.execute('SELECT credentials FROM oauth WHERE id = ?', (id,))
            row = cur.fetchone()
            if row:
                return row[0]
            return None

    def add_online_item(self, media_item, path):
        with self._db as conn:
            cur = conn.cursor()
            cur.execute('SELECT id FROM items WHERE id = "{}"'.format(media_item['id']))
            if cur.fetchone():
                log('INFO', 'Photo already in store.')
                cur.close()
                return False
            log('INFO', 'Inserting item {}'.format(media_item['id']))
            cur.close()

            creation_time = int(self._dtparse.isoparse(media_item['mediaMetadata']['creationTime']).timestamp())
            is_video = 1 if 'video' in media_item['mediaMetadata'] else 0
            conn.cursor().execute('INSERT INTO items (id, creationTime, path, mimetype, filename, video, offline) VALUES (?, ?, ?, ?, ?, ?, 0)', (media_item['id'], creation_time, path, media_item['mimeType'], media_item['filename'], is_video))
        self.record_transaction(media_item['id'], 'ADD')
        return True

    def get_items_by_downloaded(self, downloaded=False):
        """Generate items (as [id, path, filename, is_video]) that are not yet present locally."""
        with self._db as conn:
            cur = conn.cursor()
            cur.execute('SELECT id, path, filename, video FROM items WHERE offline = ? ORDER BY creationTime ASC', (1 if downloaded else 0,))
            while True:
                row = cur.fetchone()
                if not row:
                    break
                yield row

    def mark_items_downloaded(self, ids, downloaded=True):
        with self._db as conn:
            for id in ids:
                conn.cursor().execute('UPDATE items SET offline = ? WHERE id = ?', (1 if downloaded else 0, id))
                self.record_transaction(id, 'DOWNLOAD')

    def existing_items_range(self):
        with self._db as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT creationTime FROM items ORDER BY creationTime DESC LIMIT 1')
            newest = cursor.fetchone()
            cursor.execute('SELECT creationTime FROM items ORDER BY creationTime ASC LIMIT 1')
            oldest = cursor.fetchone()

            # Safe defaults that will lead to all items being selected
            old_default = datetime.datetime.now()
            new_default = datetime.datetime.fromtimestamp(0)
            return (
                datetime.datetime.fromtimestamp(int(oldest[0])) if oldest else old_default,
                datetime.datetime.fromtimestamp(int(newest[0])) if newest else new_default
            )

    def record_transaction(self, id, typ):
        """Record an event in the transaction log.

        typ should be one of 'ADD', 'DOWNLOAD'.
        """
        with self._db as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO transactions (id, type, time) VALUES (?, ?, ?)', (id, typ, int(datetime.datetime.now().timestamp())))


class Driver:
    """Coordinates synchronization.

    1. Fetch item metadata (list_library). This takes a long time on first try.
    2. Check for items not yet downloaded, download them.
    3. Start again.
    """

    def __init__(self, db, photosservice, root='', path_mapper=None):
        self._root = root
        self._db = db
        self._svc = photosservice
        self._path_mapper = path_mapper if path_mapper else Driver.path_from_date

    def fetch_metadata(self, date_range=(None, None), window_heuristic=False):
        """Fetch media metadata and write it to the database."""

        # First, figure out which ranges we need to fetch.
        ranges = [date_range]
        if not (date_range[0] or date_range[1]):
            if window_heuristic:
                (oldest, newest) = self._db.existing_items_range()
                # Special case where no previous items exist.
                if newest == datetime.datetime.fromtimestamp(0):
                    ranges = [(datetime.datetime.fromtimestamp(0), datetime.datetime.now())]
                else:
                    # Fetch from the time before the oldest item and after the newest item.
                    # This will fail if items are uploaded with a creation
                    # date in between existing items.
                    ranges = [
                        (datetime.datetime.fromtimestamp(0), oldest),
                        (newest, datetime.datetime.now())
                    ]
            else:
                ranges = [(datetime.datetime.fromtimestamp(0), datetime.datetime.now())]

        log('INFO', 'Running starting for {}'.format(date_range))

        for rng in ranges:
            for item in self._svc.list_library(start=rng[0], to=rng[1]):
                log('INFO', 'Fetched metadata for {}'.format(item['filename']))
                if self._db.add_online_item(item, self._path_mapper(item)):
                    log('INFO', 'Added {} to DB'.format(item['filename']))
        return True

    def download_items(self):
        """Scans database for items not yet downloaded and downloads them."""
        retry = []
        chunk = []
        chunksize = 16
        for item in self._db.get_items_by_downloaded(False):
            (id, path, filename, is_video) = item
            path = os.path.join(self._root, path)
            chunk.append((id, path, is_video))

            if len(chunk) > chunksize:
                ok = self._svc.download_items(chunk)
                self._db.mark_items_downloaded(ok)
                wantids = set(i[0] for i in chunk)
                missing = wantids ^ set(ok)
                for item in chunk:
                    if item[0] in missing:
                        retry.append(item)
                chunk = []

        chunk.extend(retry)
        n = chunksize
        smalls = [chunk[i:i + n] for i in range(0, len(chunk), n)]
        for chunk in smalls:
            ok = self._svc.download_items(chunk)
            self._db.mark_items_downloaded(ok)
            if len(ok) < len(chunk):
                log('WARN', 'Could not download {} items. Please try again later (photosync will automatically retry these)', len(chunk) - len(ok))

    def drive(self, date_range=(None, None), window_heuristic=True):
        """First, download all metadata since most recently fetched item.
        Then, download content."""
        # This possibly takes a long time and it may be that the user aborts in
        # between. It returns fast if most items are already present locally.
        # window_heuristic asks the metadata fetching logic to only fetch
        # items older than the oldest or newer than the newest item, which is
        # what we want for updating the items library.
        if self.fetch_metadata(date_range, window_heuristic):
            self.download_items()

    def find_vanished_items(self, dir):
        """Checks if all photos that are supposed to be downloaded are still present.

        Marks them for download otherwise, meaning that they will be downloaded later.
        """
        found = 0
        for (id, path, filename, video) in self._db.get_items_by_downloaded(downloaded=True):
            path = os.path.join(dir, path, filename)
            try:
                info = os.stat(path)
            except FileNotFoundError:
                log('INFO', 'Found vanished item at {}; marking for download', path)
                found += 1
                self._db.mark_items_downloaded([id], downloaded=False)
        if found > 0:
            log('WARN', 'Found {} vanished items. Reattempting download now...', found)
            return True
        return False


    def path_from_date(item):
        """By default, map items to year/month/day directory.

        Important: Omits the --dir relative directory (self._root).
        """
        dt = dateutil.parser.isoparser().isoparse(item['mediaMetadata']['creationTime']).date()
        return '{y}/{m:02d}/{d:02d}/'.format(y=dt.year, m=dt.month, d=dt.day)


class Main(arguments.BaseArguments):
    def __init__(self):
        doc = '''
        Download photos and videos from Google Photos. Without any arguments, photosync will check for
        new photos and download all photos that are marked as not yet downloaded as well as the new ones.

        In general, photosync works like this:

        * Download metadata for all items (initial run, or --all), or items in
          a specified date range (--dates), or before the oldest and after the
          newest item (default)
            -> items are marked as "online", i.e. not yet downloaded.
        * Check database for all items that are "online" and start download them.
        * Exit.

        This means that if you interrupt photosync during any phase of
        synchronization, it will pick up afterwards without re-executing a lot
        of work, as long as you don't use the --all option.

        Usage:
            photosync.py [options]

        Options:
            -h --help                   Show this screen
            -d --dir=<dir>              Root directory; where to download photos and store the database.
            -a --all                    Synchronize metadata for *all* photos instead of just before the oldest/after the newest photo. Needed if you have uploaded photos somewhere in the middle. Consider using --dates instead.
            --creds=<creds>             Path to the client credentials JSON file. Defaults to none. Specify to force reauth. After the first authorization, tokens are saved in the database.
            --dates=<dates>             Similar to --all, but only consider photos in the given date range: yyyy-mm-dd:yyyy-mm-dd or day: yyyy-mm-dd.
            --query=<item id>           Query metadata for item and print on console.
            --resync                    Check local filesystem for files that should be downloaded but are not there (anymore).
        '''
        super(arguments.BaseArguments, self).__init__(doc=doc)
        self.dir = self.dir or '.'
        self.creds = self.creds

    def main(self):
        # TODO: --resync, to inspect the local filesystem for vanished files.
        db = DB(os.path.join(self.dir, 'sync.db'))
        s = PhotosService(tokens=TokenSource(db=db, clientsecret=self.creds))
        d = Driver(db, s, root=self.dir)

        if self.query:
            print(s.get_item(self.query))
            return
        if self.resync:
            if d.find_vanished_items(self.dir):
                d.download_items()
                log('WARN', 'Finished downloading missing items.')
            return
        if self.all:
            d.drive(window_heuristic=False)
        elif self.dates:
            parts = self.dates.split(':')
            p = dateutil.parser.isoparser()
            window = None
            if len(parts) == 2:
                (a, b) = parts
                (a, b) = (make_date_iso(a), make_date_iso(b))
                (a, b) = p.isoparse(a), p.isoparse(b)
                window = (a, b)
            elif len(parts) == 1:
                date = p.isoparse(make_date_iso(parts[0]))
                window = (date, date)
            else:
                print("Please use --date with argument yyyy-mm-dd:yyyy-mm-dd (from:to) or yyyy-mm-dd.")
                return
            d.drive(window_heuristic=False, date_range=window)
        else:
            d.drive(window_heuristic=True)


def main():
    Main().main()


if __name__ == '__main__':
    main()
