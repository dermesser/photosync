#!/usr/bin/env python3

from setuptools import setup

setup(
        name='photosync',
        version='0.1',
        description='Download and organize media from Google Photos',
        url='https://github.com/dermesser/photosync',
        author='Lewin Bormann <lbo@spheniscida.de>',
        author_email='lbo@spheniscida.de',
        license='MIT',
        scripts=['photosync.py'],
        install_requires=[
            'arguments',
            'future',
            'consoleprinter',
            'google-api-core',
            'google-api-python-client==1.11',
            'google-auth==1.29',
            'google-auth-httplib2',
            'google-auth-oauthlib',
            'python-dateutil',
            'pyyaml',
            'requests-oauthlib',
        ])
