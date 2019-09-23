from setuptools import setup

setup(
        name='photosync',
        version='0.1',
        description='Download and organize media from Google Photos',
        url='https://github.com/dermesser/photosync',
        author='Lewin Bormann <lbo@spheniscida.de>',
        author_email='lbo@spheniscida.de',
        license='MIT',
        py_modules=['photosync'],
        install_requires=[
            'google-api-python-client',
            'google-auth-httplib2',
            'google-auth-oauthlib',
            'python-dateutil',
            'arguments',
            'future',
            'pyyaml',
            'consoleprinter',
        ])
