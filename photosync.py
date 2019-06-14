
import arguments
import datetime
import dateutil.parser
import json
import os
import os.path
import pickle
import sqlite3
import urllib3
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

PROD = False
TRACE = True


def log(level, msg):
    if PROD:
        return
    if level == 'TRACE' and not TRACE:
        return
    print (level, "::", msg)


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

    def __init__(self, db=None, tokensfile=None, clientsecret='clientsecret.json'):
        self._db = db
        self._tokensfile = tokensfile
        self._clientsecret = clientsecret

    def creds(self):
        if self._tokensfile and os.path.exists(self._tokensfile):
            with open(self._tokensfile, 'rb') as f:
                creds = pickle.load(f)
                return creds
        elif self._db:
            creds = self._db.get_credentials(self.CRED_ID)
            if creds:
                creds = pickle.loads(creds)
                return creds
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
        self._http = urllib3.PoolManager()

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

    def download_item(self, id, path, video):
        """Download a item and store it under its file name in the directory `path`.

        First, the item is queried again in order to obtain the base URL (which
        is not permanent). Then, the base URL is used to fetch the image/video
        bytes.

        Arguments:
            id: Media ID of item.
            path: Directory where to store it.
            video: Boolean, whether item is video.
        """
        item = self._service.mediaItems().get(mediaItemId=id).execute()
        rawurl = item['baseUrl']
        if video:
            rawurl = '{url}=dv'.format(url=rawurl)
        else:
            rawurl = '{url}=d'.format(url=rawurl)
        os.makedirs(path, exist_ok=True)
        p = os.path.join(path, item['filename'])
        with open(p, 'wb') as f:
            f.write(self._http.request('GET', rawurl).data)


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

    def get_not_downloaded_items(self):
        """Generate items (as [id, path, filename, is_video]) that are not yet present locally."""
        with self._db as conn:
            cur = conn.cursor()
            cur.execute('SELECT id, path, filename, video FROM items WHERE offline = 0 ORDER BY creationTime ASC')
            while True:
                row = cur.fetchone()
                if not row:
                    break
                yield row

    def mark_item_downloaded(self, id):
        with self._db as conn:
            conn.cursor().execute('UPDATE items SET offline = 1 WHERE id = ?', (id,))
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
        for item in self._db.get_not_downloaded_items():
            (id, path, filename, is_video) = item
            path = os.path.join(self._root, path)
            log('INFO', 'Downloading {fn} into {p}'.format(fn=filename, p=path))
            self._svc.download_item(id, path, is_video)
            log('INFO', 'Downloading {fn} successful'.format(fn=filename))
            self._db.mark_item_downloaded(id)

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

    def path_from_date(item):
        """By default, map items to year/month/day directory."""
        dt = dateutil.parser.isoparser().isoparse(item['mediaMetadata']['creationTime']).date()
        return '{y}/{m:02d}/{d:02d}/'.format(y=dt.year, m=dt.month, d=dt.day)


class Main(arguments.BaseArguments):
    def __init__(self):
        doc = '''
        Download photos and videos from Google Photos.

        Usage:
            photosync.py [options]

        Options:
            -h --help                   Show this screen
            -d --dir=<dir>              Root directory; where to download photos and store the database.
            --creds=clientsecret.json   Path to the client credentials JSON file. Defaults to
            --all                       Synchronize *all* photos instead of just before the oldest/after the newest photo. Needed if you have uploaded photos somewhere in the middle.
        '''
        super(arguments.BaseArguments, self).__init__(doc=doc)
        self.dir = self.dir or '.'
        self.creds = self.creds or 'clientsecret.json'

    def main(self):
        # TODO: --resync, to inspect the local filesystem for vanished files.
        db = DB(os.path.join(self.dir, 'sync.db'))
        s = PhotosService(tokens=TokenSource(db=db, clientsecret=self.creds))
        d = Driver(db, s, root=self.dir)
        if self.all:
            d.drive(window_heuristic=False)
        else:
            d.drive(window_heuristic=True)


def main():
    Main().main()


if __name__ == '__main__':
    main()
