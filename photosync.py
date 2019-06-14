
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
    print(level, "::", msg)


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
            resp = self._service.mediaItems().search(body={'pageSize': 25, 'filters': filters, 'pageToken': pagetoken}).execute()
            pagetoken = resp.get('nextPageToken', None)
            items = resp.get('mediaItems', None)
            if not items:
                return
            for i in items:
                log('TRACE', i['mediaMetadata']['creationTime'])
                yield i
            if pagetoken is None:
                return

    def download_photo(self, id, path):
        """Download a photo and store it under its file name in the directory `path`.
        """
        photo = self._service.mediaItems().get(mediaItemId=id).execute()
        rawurl = photo['baseUrl']
        rawurl = '{url}=d'.format(url=rawurl)
        os.makedirs(path, exist_ok=True)
        p = os.path.join(path, photo['filename'])
        with open(p, 'wb') as f:
            f.write(self._http.request('GET', rawurl).data)


class DB:

    def __init__(self, path):
        self._db = sqlite3.connect(path)
        self.initdb()
        self._dtparse = dateutil.parser.isoparser()

    def initdb(self):
        cur = self._db.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS photos (id TEXT PRIMARY KEY, creationTime TEXT, path TEXT, filename TEXT, offline INTEGER)')
        cur.execute('CREATE TABLE IF NOT EXISTS transactions (id TEXT, type TEXT, time INTEGER, path TEXT, filename TEXT)')
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

    def add_online_photo(self, media_item, path):
        with self._db as conn:
            cur = conn.cursor()
            cur.execute('SELECT id FROM photos WHERE id = "{}"'.format(media_item['id']))
            if cur.fetchone():
                log('INFO', 'Photo already in store.')
                cur.close()
                return False
            log('INFO', 'Inserting photo {}'.format(media_item['id']))
            cur.close()

            creation_time = int(self._dtparse.isoparse(media_item['mediaMetadata']['creationTime']).timestamp())
            conn.cursor().execute('INSERT INTO photos (id, creationTime, path, filename, offline) VALUES (?, ?, ?, ?, 0)', (media_item['id'], creation_time, path, media_item['filename']))
            conn.commit()
            return True

    def get_not_downloaded_photos(self):
        """Yield photos (as [id, path]) that are not yet present locally."""
        with self._db as conn:
            cur = conn.cursor()
            cur.execute('SELECT id, path, filename FROM photos WHERE offline = 0 ORDER BY creationTime ASC')
            while True:
                row = cur.fetchone()
                if not row:
                    break
                yield row

    def mark_photo_downloaded(self, id):
        with self._db as conn:
            conn.cursor().execute('UPDATE photos SET offline = 1 WHERE id = ?', (id,))

    def most_recent_creation_date(self):
        with self._db as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT creationTime FROM photos ORDER BY creationTime DESC LIMIT 1')
            row = cursor.fetchone()
            cursor.close()
            if row:
                return datetime.datetime.fromtimestamp(int(row[0]))
            return datetime.datetime.fromtimestamp(0)


class Driver:
    """Coordinates synchronization.

    1. Fetch photo metadata (list_library). This takes a long time on first try.
    2. Check for photos not yet downloaded, download them.
    3. Start again.
    """

    def __init__(self, db, photosservice, path_mapper=None):
        self._db = db
        self._svc = photosservice
        self._path_mapper = path_mapper if path_mapper else Driver.path_from_date

    def fetch_metadata(self, date_range=(None, None), start_at_recent=False):
        """Fetch media metadata and write it to the database."""
        if not (date_range[0] or date_range[1]):
            if start_at_recent:
                date_range = (self._db.most_recent_creation_date(), datetime.datetime.now())
        log('INFO', 'Running starting for {}'.format(date_range))

        for photo in self._svc.list_library(start=date_range[0], to=date_range[1]):
            log('INFO', 'Fetched metadata for {}'.format(photo['filename']))
            if self._db.add_online_photo(photo, self._path_mapper(photo)):
                log('INFO', 'Added {} to DB'.format(photo['filename']))
        return True

    def download_photos(self):
        """Scans database for photos not yet downloaded and downloads them."""
        for photo in self._db.get_not_downloaded_photos():
            (id, path, filename) = photo
            log ('INFO', 'Downloading {fn} into {p}'.format(fn=filename, p=path))
            self._svc.download_photo(id, path)
            log('INFO', 'Downloading {fn} successful'.format(fn=filename))
            self._db.mark_photo_downloaded(id)

    def drive(self, date_range=(None, None), start_at_recent=True):
        """First, download all metadata since most recently fetched photo.
        Then, download content."""
        # This possibly takes a long time and it may be that the user aborts in
        # between. It returns fast if most photos are already present locally.
        if self.fetch_metadata(date_range, start_at_recent):
            self.download_photos()

    def path_from_date(item):
        """By default, map photos to year/month/day directory."""
        dt = dateutil.parser.isoparser().isoparse(item['mediaMetadata']['creationTime']).date()
        return '{y}/{m:02d}/{d:02d}/'.format(y=dt.year, m=dt.month, d=dt.day)


def main():
    db = DB('photosync.db')
    s = PhotosService(tokens=TokenSource(db=db))
    d = Driver(db, s)
    d.drive()

if __name__ == '__main__':
    main()
