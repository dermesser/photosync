# photosync

Now that Google deprecated the Photos<-\>Drive synchronization, I need another way to back up my photos locally. This
program downloads all photos from your Google Photos account and organizes them locally. It is not very user friendly
yet, but definitely usable.

photosync only ever downloads photos, i.e. the synchronization works from Google Photos as Source of Truth to your local
storage. Don't worry about deleting photos locally; although you have to use the slow `--resync` option to re-download them.

photosync is fast enough for reasonably large libraries. My library of ~50'000 photos was indexed in roughly an hour;
the (resumable) download takes another few hours, depending on latency and photo size.

**Pull requests are welcome!**

## Behavior

By default, photosync will ask for OAuth2 authorization on the console, and then immediately start downloading metadata
from Google Photos. Once no more new photos are fetched and all metadata is stored in `sync.db`, photosync will look for
photos that are not yet marked as downloaded in the database and fetch the actual image files. By default, it will
organize photos in directories like `year/month/day/` (numerically, 0-padded), but you can write your own method of
mapping photos to directories and override it by setting the `path_mapper` argument in the `Driver` constructor called
from `Main.main()`.

Note that this (obviously) takes a while for large libraries. But you can always stop photosync and restart it later;
without the `--all` option, it will resume synchronization where it left off.

Albums are currently ignored. Videos are downloaded just like photos.

## Install & Use

First, acquire a client secret. This is necessary because this is an open source project, and I don't want client
credentials associated with my account floating around in the wild. Also, the daily limit for Photos API calls is at
10'000, so it wouldn't work for a nontrivial number of users anyway.

For this,

1. go to https://console.developers.google.com.
1. Ensure you are on the right project or create a new one
1. Go to the APIs page and enable the Google Photos API.
1. Set up the OAuth consent screen (otherwise Google will nag you during credentials creation to do it).
1. Then go to the *Credentials* page and create a new client ID (type `other`). Download the JSON file using the
   download button at the right hand side.
1. Save the downloaded JSON file and put it somewhere, for example in your photos directory. Pass the path to the file
   to photosync using the `--creds` argument. By default, photosync will look for a file called `clientsecret.json` in
   the current directory.
1. After the first run, credentials are cached in the internal SQLite database,
   meaning you don't have to explicitly specify them on any further invocations.

Once you have gone through the hassle of obtaining the client secret, you can start downloading your photos.

1. Clone this repository to a convenient place: `git clone https://github.com/dermesser/photosync` or `hg clone
   git+https://github.com/dermesser/photosync`.
1. Go into the `photosync` repository and run `pip[3] install [--user] .`. This
   installs dependencies needed by the program.
   - Or you use `pipenv` using the provided `Pipfile`: `pipenv shell && pipenv
   install`, after which you can use `python photosync.py`.
   - Alternatively, build an egg `python setup.py bdist_egg` and install it
   `easy_install dist/photosync-[version]-py[version].egg`
1. Run it: `python3 photosync.py --help` or, if you installed it with `pip`,
   `photosync.py --help`.

Consult the help text printed by the last command. Usually you will need to set
`--dir` so that your photos don't end up in the current directory. Typically you
would initially run

```
$ python3 photosync.py --dir=/target/directory --creds=/path/to/clientsecret.json
```

which also asks you for OAuth authorization. After having uploaded photos
(careful: Google Photos exposes new media only a few minutes up to half an hour
 after uploading!) you can run the following command, which looks for photos and
videos that are either older than the oldest known item or newer than the newest
known item (obviously missing any that have been uploaded with a date between
the oldest and newest items: see `--dates` below how to fix it):

```
$ python3 photosync.py --dir=/target/directory
```

If it turns out you are missing some items locally, you can check Google Photos
again for them:

```
# to check *all* photos available for January 2000:
$ python3 photosync.py --dir=/target/directory --dates=2000-01-01:2000-02-01

# to check *all* photos from *ever*:
$ python3 photosync.py --dir=/target/directory --all
```

## Troubleshooting

* I have seen `Invalid media item ID.` errors for valid-looking media item IDs. This happened to a handful of photos,
  all from the same day. The media item IDs all started with the same prefix which was different than the shared prefix of
  all other media item IDs (all IDs from one account usually start with the same 4-6 characters). I'm not sure why the
  API at one point returned those.
  * To clean this up, remove the invalid IDs from the database (`sqlite3 sync.db "DELETE FROM items WHERE id LIKE
    'wrongprefix%'"`) after checking that only a small number of items has this kind of ID (`sqlite3 sync.db "SELECT *
    FROM items WHERE id LIKE 'wrongprefix%'"`).
  * Re-fetch metadata for the affected days: `python3 photosync.py --dir=.../directory --all --dates=2012-12-12:2012-12-14`
    (for example)
