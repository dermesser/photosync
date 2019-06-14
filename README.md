# photosync

Now that Google deprecates the Photos<-\>Drive synchronization, I need another way to back up my photos locally. This
program downloads all photos from your Google Photos account and organizes them locally. It is not very user friendly
yet, but definitely usable.

**Pull requests are welcome!**

## Behavior

By default, photosync will ask for OAuth2 authorization on the console, and then immediately start downloading metadata
from Google Photos. Once no more new photos are fetched and all metadata is stored in `photosync.db`, photosync will
look for photos that are not yet marked as downloaded in the database and fetch the actual image files. By default, it
will organize photos in directories like `year/month/day/` (numerically, 0-padded), but you can write your own method of
mapping photos to directories.

Albums are currently ignored.

## Install & Use

1. Clone this repository to a convenient place: `git clone https://github.com/dermesser/photosync` or `hg clone
   git+https://github.com/dermesser/photosync`.
1. Install `pipenv`: `pip3 install pipenv` if not yet installed.
1. Go into the `photosync` repository and run `pipenv install` (make sure that `~/.local/bin/pipenv` is in your `PATH`)
1. Activate the virtualenv: `pipenv shell` (sigh)
1. Run it: `python3 photosync.py --help`

Consult the help text printed by the last command. Usually you will need to set `--dir` so that your photos don't end up
in the repository.
