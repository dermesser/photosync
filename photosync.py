
import datetime
import dateutil.parser
import json
import os.path
import pickle
import sqlite3
import urllib3
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

class TokenSource:
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
            if start:
                rng_filter['ranges']['startDate'] = {'year': start.year, 'month': start.month, 'day': start.day}
            else:
                rng_filter['ranges']['startDate'] = {'year': 1999, 'month': 1, 'day': 1}
            if not to:
                to = datetime.datetime.now().date()
            rng_filter['ranges']['endDate'] = {'year': to.year, 'month': to.month, 'day': to.day}
            filters['dateFilter'] = rng_filter
        pagetoken = None
        while True:
            resp = self._service.mediaItems().search(body={'pageSize': 25, 'filters': filters, 'pageToken': pagetoken}).execute()
            items = resp['mediaItems']
            pagetoken = resp.get('nextPageToken', None)
            for i in items:
                yield i
            if pagetoken is None:
                return

    def download_photo(self, id, path):
        """Download a photo and store it under its file name in the directory `path`.
        """
        photo = self._service.mediaItems().get(mediaItemId=id).execute()
        rawurl = photo['baseUrl']
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
                print('WARN: Photo already in store.')
                cur.close()
                return
            cur.close()

            creation_time = int(self._dtparse.isoparse(media_item['mediaMetadata']['creationTime']).timestamp())
            conn.cursor().execute(
                    'INSERT INTO photos (id, creationTime, path, filename, offline) VALUES (?, ?, ?, ?, 0)',
                    media_item['id'], creation_time, path, media_item['filename'])
            conn.commit()

    def mark_photo_downloaded(self, id):
        with self._db as conn:
            conn.cursor().execute(
                    'UPDATE photos SET offline = 1 WHERE id = {}'.format(id))

    def most_recent_creation_date(self):
        with self._db as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT creationTime FROM photos ORDER BY creationTime DESC LIMIT 1')
            row = cursor.fetchone()
            cursor.close()
            if row:
                return datetime.datetime.fromtimestamp(row[0])
            return datetime.datetime.fromtimestamp(0)

def main():
    db = DB('sq.lite')
    s = PhotosService(tokens=TokenSource(db=db))
    items = s.list_library(to=datetime.date(2019, 7, 7))
    for i in items:
        db.add_online_photo(i, 'local')

if __name__ == '__main__':
    main()
