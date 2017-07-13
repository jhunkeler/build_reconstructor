#!/usr/bin/env python

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from distutils.spawn import find_executable
from tarfile import TarFile
from tempfile import TemporaryDirectory

try:
    from conda_build.metadata import MetaData
except ImportError:
    print('Missing conda-build:\n\t$ conda install conda-build')
    exit(1)

if not find_executable('sloccount'):
    print('Missing sloccount:\n\thttps://www.dwheeler.com/sloccount/')
    exit(1)

BAD_MAGIC = 0xFACEFEED
ESCAPE_CHARS = '\\;&|'


class GitError(Exception):
    pass

class Package(object):
    def __init__(self, filename):
        valid_uris = ['file:', 'http:', 'https:']
        self.filename = filename
        self.remote_file = False
        self.old_behavior = False
        self.source_type = None

        for uri in valid_uris:
            if self.filename.startswith(uri):
                self.remote_file = True
                break

        if not self.remote_file:
            if not os.path.exists(self.filename):
                raise FileNotFoundError(self.filename)

        self.META_TEMPLATE_TARGET = 'info/recipe/meta.yaml.template'
        self.META_TARGET = 'info/recipe/meta.yaml'
        self.metadata = self._populate_metadata()

    def _populate_metadata(self):
        with TemporaryDirectory() as tempdir:
            meta_src = os.path.join(tempdir, self.META_TEMPLATE_TARGET)
            meta_dest = os.path.join(tempdir, self.META_TARGET)
            tarball_dest = os.path.join(tempdir,
                                        os.path.basename(self.filename))

            if self.remote_file:
                download(self.filename, tarball_dest)

            print('Extracting {0}'.format(tarball_dest))
            try:
                with TarFile.open(tarball_dest, 'r:*') as tarball:
                    tarball.extract(self.META_TEMPLATE_TARGET, tempdir)
            except KeyError:
                try:
                    # Older version of conda-build: Use META_TARGET instead
                    self.old_behavior = True
                    with TarFile.open(tarball_dest, 'r:*') as tarball:
                        tarball.extract(self.META_TARGET, tempdir)
                except KeyError:
                    print('Proprietary package lacks required data!')
                    print()
                    return None

            if not self.old_behavior:
                shutil.move(meta_src, meta_dest)

            print()
            return MetaData(meta_dest)

    def version(self):
        global BAD_MAGIC

        _, base, _ = os.path.basename(self.filename).split('-')

        if '.dev' in base:
            tag, post_commit = base.split('.dev')
        elif 'dev' in base:
            # astropy is the ONLY package with this problem
            # not surprised.
            tag, post_commit = base.split('dev')
            major, minor = tag.split('.')
            major = int(major)
            minor = int(minor)
            minor -= 1
            tag = 'v' + '.'.join([str(major), str(minor)])
            post_commit = (int(post_commit) << 32) | BAD_MAGIC
        else:
            return base, 0

        return tag, int(post_commit)

    def source_url(self):
        '''Only accounts for git repos and tarballs, beware.'''
        # assume git mode
        url = self.metadata.get_value('source/git_url')
        self.source_type = 'git'

        # if that's not the case, try for an archive instead
        if not url:
            url = self.metadata.get_value('source/url')

            # Handle bizarre case if recipe's source/url contains a list
            # instead of a string
            if isinstance(url, list):
                url = url[0]

            self.source_type = 'archive'

        return url


class SpecFileError(Exception):
    pass


class SpecFileFormatError(Exception):
    pass


class SpecFile(object):
    def __init__(self, filename, include_only=[], include_only_urls=[]):
        assert isinstance(include_only, list)

        self.filename = filename
        self.urls = []
        self.include_pkgs = []
        self.include_urls = []
        self.data = []

        self.check_format()

        with open(self.filename, 'r') as fp:
            self.data = fp.read().splitlines()

        if not self.data:
            raise SpecFileError('Spec file contains no data.')

        for url in self.data:
            url = url.rstrip()
            tarball = os.path.basename(url)

            if not url or url.startswith('#') or url.startswith('@'):
                continue

            if include_only_urls:
                for pattern in include_only_urls:
                    if pattern in url:
                        self.include_urls.append(url)

            if include_only:
                for pattern in include_only:
                    if tarball.startswith(pattern + '-'):
                        # If URL already exists, don't add it again
                        if pattern not in self.urls:
                            self.include_pkgs.append(url)

            if include_only and include_only_urls:
                self.urls = self.include_pkgs
                continue
            elif include_only:
                self.urls = self.include_pkgs
                continue
            elif include_only_urls:
                self.urls = self.include_urls
                continue
            else:
                self.urls.append(url)

    def check_format(self):
        with open(self.filename) as fp:
            data = fp.read()

        if '@EXPLICIT' not in data:
            raise SpecFileFormatError('{0} is not a valid environment '
                                      'dump file.'.format(self.filename))

    def __iter__(self):
        for x in self.urls:
            yield x


def download(url, dest):
    assert isinstance(url, str)
    assert isinstance(dest, str)

    print('Downloading {0}'.format(url))
    # try/except something here... docs are unclear
    path, _ = urllib.request.urlretrieve(url, dest)
    return path


def untar(path, dest='.'):
    print('Extracting {0}'.format(path))
    with TarFile.open(path, 'r:*') as tarball:
        tarball.extractall(dest)


def git(task, *args):
    assert isinstance(task, str)
    for arg in args:
        assert isinstance(arg, str)

    cmd = ['git', task]
    tasks = ['clone', 'checkout', 'log', 'fetch']

    for arg in args:
        cmd.append(arg)

    if not safe_command(cmd):
        raise ValueError('Unsafe execution attempt. '
                         'Refusing to execute: "{0}"'.format(cmd))

    if task not in tasks:
        raise ValueError('Invalid task: "{0}". Must be one of: '
                         '{1}'.format(task, ', '.join([x for x in tasks])))

    print('Running: {0}'.format(' '.join(cmd)))
    try:
        output = subprocess.check_output(cmd,
                                         stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError as e:
        raise GitError(e)

    return output


def git_clone(url, path=''):
    git('clone', url, path)


def git_checkout(path, rev):
    assert isinstance(path, str)
    assert isinstance(rev, str)

    cwd = os.path.abspath(os.curdir)
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    os.chdir(path)
    git('checkout', rev)
    os.chdir(cwd)


def git_commit_from_offset(path, tag, post_commit):
    assert isinstance(tag, str)
    assert isinstance(post_commit, int)

    cwd = os.path.abspath(os.curdir)
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    os.chdir(path)
    name = os.path.basename(path).split('-')[0]
    tags = [tag, 'v' + tag, '-'.join([name, tag]), name + '-' + 'v' + tag]
    offset = tag + '..' + 'master'
    result = ''

    if not post_commit:
        for salvo in tags:
            try:
                result = git('log', '--oneline', salvo).splitlines()[0]
                break
            except GitError:
                continue

        rev, message = result.split(' ', 1)
        os.chdir(cwd)
        return rev

    magic_post_commit, bad_magic = filter_commit(post_commit)
    if bad_magic:
        result = git('log', '--oneline').splitlines()
        rev, message = result[magic_post_commit].split(' ', 1)
        os.chdir(cwd)
        return rev

    for salvo in tags:
        try:
            result = git('log', '--oneline', salvo).splitlines()
            break
        except GitError:
            continue

    tail = len(result)
    rev, message = result[tail - post_commit].rstrip().split(' ', 1)

    os.chdir(cwd)
    return rev


def filter_commit(x):
    has_magic = False
    if (x & 0xffffffff) == BAD_MAGIC:
        print('BAD_MAGIC ({0:#08x}) detected!'.format(BAD_MAGIC))
        x >>= 32
        has_magic = True

    return x, has_magic


def safe_command(x):
    assert isinstance(x, list)

    for ch in ESCAPE_CHARS:
        cmdtmp = ' '.join(x)
        if ch in cmdtmp:
            return False

    return True

def copytree(src, dst, symlinks=False):
    names = os.listdir(src)
    os.makedirs(dst)
    errors = []
    for name in names:
        srcname = os.path.join(src, name)
        dstname = os.path.join(dst, name)
        try:
            if symlinks and os.path.islink(srcname):
                linkto = os.readlink(srcname)
                os.symlink(linkto, dstname)
            elif os.path.isdir(srcname):
                shutil.copytree(srcname, dstname, symlinks)
            else:
                shutil.copy2(srcname, dstname)
            # XXX What about devices, sockets etc.?
        except OSError as why:
            errors.append((srcname, dstname, str(why)))
        # catch the Error from the recursive copytree so that we can
        # continue with other files
        except shutil.Error as err:
            errors.extend(err.args[0])
    try:
        shutil.copystat(src, dst)
    except OSError as why:
        # can't copy file access times on Windows
        if why.winerror is None:
            errors.extend((src, dst, str(why)))
    if errors:
        raise Error(errors)

def sloccount(path):
    cmd = ['sloccount', '--multiproject', path]

    if not safe_command(cmd):
        raise ValueError('Unsafe execution attempt. '
                         'Refusing to execute: "{0}"'.format(cmd))

    print('Running: {0}'.format(' '.join(cmd)))
    output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()

    return output

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('specfile',
                        action='store',
                        help='EXPLICIT environment dump file')
    parser.add_argument('-p',
                        '--include-pkgs',
                        nargs='+',
                        type=str,
                        default=[],
                        action='store',
                        help='Filter parsed packages by name')
    parser.add_argument('-u',
                        '--include-urls',
                        nargs='+',
                        type=str,
                        default=[],
                        action='store',
                        help='Filter parsed URLs by name')
    parser.add_argument('-k',
                        '--keep-files',
                        action='store_true',
                        help='Retain a copy of the work directory')
    args = parser.parse_args()

    packages = []
    specfile = SpecFile(args.specfile, args.include_pkgs, args.include_urls)


    if not specfile.urls:
        print('Nothing to do.')
        exit(1)

    for url in specfile:
        pkg = Package(url)
        packages.append(pkg)

    with TemporaryDirectory() as tempdir:
        total_processed = 0
        total_skipped = 0
        for pkg in packages:
            if pkg.metadata is None:
                print('No metadata. Skipping {0}'.format(pkg.filename))
                total_skipped += 1
                continue

            url = pkg.source_url()

            if not url:
                print('Unknown type for source URL: {0}. Skipping {1}'
                      .format(pkg.metadata.name(), pkg.filename))
                total_skipped += 1
                continue

            if pkg.source_type == 'git':
                tag, post_commit = pkg.version()

                # Sanitize post-commit information
                # (why does everything need a hack?)
                magic_post_commit, bad_magic = filter_commit(post_commit)

                # I tried to consolidate this block but smoke poured out
                # TODO: Improve this
                if bad_magic:
                    dest = os.path.join(tempdir, '-'.join([pkg.metadata.name(),
                                        tag, str(magic_post_commit)]))
                else:
                    dest = os.path.join(tempdir, '-'.join([pkg.metadata.name(),
                                        tag, str(post_commit)]))

                git_clone(url, dest)
                offset = git_commit_from_offset(dest, tag, post_commit)
                git_checkout(dest, offset)

            elif pkg.source_type == 'archive':
                with TemporaryDirectory() as tempdir2:
                    dest = os.path.join(tempdir2, os.path.basename(url))
                    archive = download(url, dest)
                    untar(archive, tempdir)

            total_processed += 1
            print()

        print('\nProccessed: {0}\nSkipped: {1}\n'.format(total_processed, total_skipped))

        if total_processed:
            result = sloccount(tempdir)
            print(result)
        else:
            print('sloccount report not generated.')
            exit(1)

        if args.keep_files:
            kdir = '-'.join([os.path.basename(os.path.splitext(args.specfile)[0]), 'BR' ,str(int(time.time()))])
            copytree(tempdir, kdir, symlinks=True)

        exit(0)
