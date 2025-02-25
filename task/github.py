# This file is part of Cockpit.
#
# Copyright (C) 2015 Red Hat, Inc.
#
# Cockpit is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation; either version 2.1 of the License, or
# (at your option) any later version.
#
# Cockpit is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Cockpit; If not, see <http://www.gnu.org/licenses/>.

# Shared GitHub code. When run as a script, we print out info about
# our GitHub interacition.

import http.client
import json
import logging
import os
import re
import socket
import subprocess
import time
import urllib.parse
from http import HTTPStatus
from ssl import SSLEOFError

from lib.directories import xdg_cache_home, xdg_config_home
from lib.testmap import is_valid_context

from . import cache

__all__ = (
    'GitHub',
    'GitHubError',
    'Checklist',
    'TESTING',
    'NO_TESTING',
    'NOT_TESTED',
    'NOT_TESTED_DIRECT',
)

TESTING = "Testing in progress"
NO_TESTING = "Manual testing required"

# if the webhook receives a pull request event, it will create a status for each
# context with NOT_TESTED as description
# the subsequent status events caused by the webhook creating the statuses, will
# be ignored by the webhook as it only handles NOT_TESTED_DIRECT as described
# below
NOT_TESTED = "Not yet tested"
# if the webhook receives a status event with NOT_TESTED_DIRECT as description,
# it will publish a test task to the queue (used to trigger specific contexts)
NOT_TESTED_DIRECT = "Not yet tested (direct trigger)"

ISSUE_TITLE_IMAGE_REFRESH = "Image refresh for {0}"


class Logger(object):
    def __init__(self, directory):
        hostname = socket.gethostname().split(".")[0]
        month = time.strftime("%Y%m")
        self.path = os.path.join(directory, f"{hostname}-{month}.log")

        os.makedirs(directory, exist_ok=True)

    # Yes, we open the file each time
    def write(self, value):
        with open(self.path, 'a') as f:
            f.write(value)


class GitHubError(RuntimeError):
    """Raise when getting an error from the GitHub API

    We used to raise `RuntimeError` before. Subclass from that, so that client
    code depending on it continues to work.
    """

    def __init__(self, url, response):
        self.url = url
        self.data = response.get('data', "")
        self.status = response.get('status')
        self.reason = response.get('reason')

    def __str__(self):
        result = (f'Error accessing {self.url}\n'
                  f'  Status: {self.status}\n'
                  f'  Reason: {self.reason}\n'
                  f'  Response: {self.data}')

        if self.status == 401:
            result += ('\n\nPlease ensure that your github-token is configured '
                       'with the appropriate permissions.  See bots/README.md')

        return result


def get_repo():
    res = subprocess.check_output(['git', 'config', '--default=', 'cockpit.bots.github-repo'])
    return res.decode('utf-8').strip() or None


def get_origin_repo():
    try:
        res = subprocess.check_output(["git", "remote", "get-url", "origin"])
    except subprocess.CalledProcessError:
        return None
    url = res.decode('utf-8').strip()
    m = re.fullmatch("(git@github.com:|https://github.com/)(.*?)(\\.git)?", url)
    if m:
        return m.group(2).rstrip("/")
    raise RuntimeError("Not a GitHub repo: %s" % url)


class GitHub(object):
    def __init__(self, base=None, cacher=None, repo=None, remote=None):
        self._remote = remote
        self._repo = repo
        self._base = base
        self._url = None

        self.conn = None
        self.token = None
        self.debug = False
        try:
            with open(xdg_config_home('cockpit-dev', 'github-token'), "r") as f:
                self.token = f.read().strip()
        except FileNotFoundError:
            try:
                with open(xdg_config_home('github-token'), "r") as f:
                    self.token = f.read().strip()
            except FileNotFoundError:
                # fall back to GitHub's CLI token
                try:
                    with open(xdg_config_home("gh/config.yml")) as f:
                        match = re.search(r'oauth_token:\s*(\S+)', f.read())
                    if match:
                        self.token = match.group(1)
                except FileNotFoundError:
                    # token not found anywhere, so only reading operations are available
                    pass

        # default cache directory
        if not cacher:
            cacher = cache.Cache(xdg_cache_home('github'))

        self.cache = cacher

        # Create a log for debugging our GitHub access
        self.log = Logger(self.cache.directory)
        self.log.write("")

    @property
    def remote(self):
        if not self._remote:
            repo = os.environ.get("GITHUB_BASE", None) or get_repo()

            if repo:
                self._remote = f'https://github.com/{repo}'
            else:
                self._remote = 'origin'

        return self._remote

    @property
    def repo(self):
        if not self._repo:
            self._repo = os.environ.get("GITHUB_BASE", None) or get_repo() or get_origin_repo()
            if not self._repo:
                raise RuntimeError('Could not determine the github repository:\n'
                                   '  - some commands accept a --repo argument\n'
                                   '  - you can set the GITHUB_BASE environment variable\n'
                                   '  - you can set git config cockpit.bots.github-repo\n'
                                   '  - otherwise, the "origin" remote from the current checkout is used')

        return self._repo

    @property
    def url(self):
        if not self._url:
            if not self._base:
                netloc = os.environ.get("GITHUB_API", "https://api.github.com")
                self._base = f"{netloc}/repos/{self.repo}"

            self._url = urllib.parse.urlparse(self._base)

        return self._url

    def qualify(self, resource):
        if resource is None:
            return self.url.path
        return urllib.parse.urljoin(f"{self.url.path}/", resource)

    def request(self, method, resource, data="", headers=None):
        if headers is None:
            headers = {}
        headers["User-Agent"] = "Cockpit Tests"
        if self.token:
            headers["Authorization"] = "token " + self.token

        for retry in range(5):
            if not self.conn:
                if self.url.scheme == 'http':
                    self.conn = http.client.HTTPConnection(self.url.netloc)
                else:
                    self.conn = http.client.HTTPSConnection(self.url.netloc)
                self.conn.set_debuglevel(self.debug and 1 or 0)

            try:
                self.conn.request(method, self.qualify(resource), data, headers)
                response = self.conn.getresponse()
                if response.status != HTTPStatus.BAD_GATEWAY:
                    # success!
                    break
            except (ConnectionResetError, http.client.BadStatusLine, socket.error, SSLEOFError) as e:
                logging.warning("Transient error during GitHub request, attempt #%s: %s", retry, e)

            self.conn = None
            time.sleep(2 ** retry)
        else:
            raise IOError("Repeated failure to talk to GitHub API, giving up")

        heads = {}
        for (header, value) in response.getheaders():
            heads[header.lower()] = value
        self.log.write('{0} - - [{1}] "{2} {3} HTTP/1.1" {4} -\n'.format(
            self.url.netloc,
            time.asctime(),
            method,
            resource,
            response.status
        ))
        return {
            "status": response.status,
            "reason": response.reason,
            "headers": heads,
            "data": response.read().decode('utf-8')
        }

    def get(self, resource=None):
        headers = {}
        qualified = self.qualify(resource)
        cached = self.cache.read(qualified)
        if cached:
            if self.cache.current(qualified):
                return json.loads(cached['data'] or "null")
            etag = cached['headers'].get("etag", None)
            modified = cached['headers'].get("last-modified", None)
            if etag:
                headers['If-None-Match'] = etag
            elif modified:
                headers['If-Modified-Since'] = modified
        response = self.request("GET", resource, "", headers)
        if response['status'] == 404:
            return None
        elif cached and response['status'] == 304:  # Not modified
            self.cache.write(qualified, cached)
            return json.loads(cached['data'] or "null")
        elif response['status'] < 200 or response['status'] >= 300:
            raise GitHubError(self.qualify(resource), response)
        else:
            self.cache.write(qualified, response)
            return json.loads(response['data'] or "null")

    def post(self, resource, data, accept=[]):
        response = self.request("POST", resource, json.dumps(data), {"Content-Type": "application/json"})
        status = response['status']
        if (status < 200 or status >= 300) and status not in accept:
            raise GitHubError(self.qualify(resource), response)
        self.cache.mark()
        return json.loads(response['data'])

    def put(self, resource, data, accept=[]):
        response = self.request("PUT", resource, json.dumps(data), {"Content-Type": "application/json"})
        status = response['status']
        if (status < 200 or status >= 300) and status not in accept:
            raise GitHubError(self.qualify(resource), response)
        self.cache.mark()
        if response['data']:
            return json.loads(response['data'])
        else:
            return None

    def delete(self, resource, accept=[]):
        response = self.request("DELETE", resource, "", {"Content-Type": "application/json"})
        status = response['status']
        if (status < 200 or status >= 300) and status not in accept:
            raise GitHubError(self.qualify(resource), response)
        self.cache.mark()
        if response['data']:
            return json.loads(response['data'])
        else:
            return None

    def patch(self, resource, data, accept=[]):
        response = self.request("PATCH", resource, json.dumps(data), {"Content-Type": "application/json"})
        status = response['status']
        if (status < 200 or status >= 300) and status not in accept:
            raise GitHubError(self.qualify(resource), response)
        self.cache.mark()
        return json.loads(response['data'])

    def statuses(self, revision):
        result = {}
        page = 1
        count = 100
        while count == 100:
            data = self.get(f"commits/{revision}/status?page={page}&per_page={count}")
            count = 0
            page += 1
            if "statuses" in data:
                for status in data["statuses"]:
                    if is_valid_context(status["context"], self.repo) and status["context"] not in result:
                        result[status["context"]] = status
                count = len(data["statuses"])
        return result

    def all_statuses(self, revision):
        result = []
        page = 1
        count = 100
        while count == 100:
            data = self.get(f"commits/{revision}/statuses?page={page}&per_page={count}")
            count = 0
            page += 1
            result += data
            count = len(data)
        return result

    def pulls(self, state='open', since=None):
        result = []
        page = 1
        count = 100
        while count == 100:
            pulls = self.get(f"pulls?page={page}&per_page={count}&state={state}&sort=created&direction=desc")
            count = 0
            page += 1
            for pull in pulls or []:
                # Check that the pulls are past the expected date
                if since:
                    closed = pull.get("closed_at", None)
                    if closed and since > time.mktime(time.strptime(closed, "%Y-%m-%dT%H:%M:%SZ")):
                        continue
                    created = pull.get("created_at", None)
                    if not closed and created and since > time.mktime(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ")):
                        continue

                result.append(pull)
                count += 1
        return result

    # The since argument is seconds since the issue was last time modified
    def issues(self, labels=["bot"], state="open", since=None):
        result = []
        page = 1
        count = 100
        label = ",".join(labels)
        if since:
            since = "&since={0}".format(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)))
        else:
            since = ""

        while count == 100:
            req = f"issues?labels={label}&state={state}&page={page}&per_page={count}{since}"
            issues = self.get(req)

            page += 1
            count = len(issues)
            result = result + issues
        return result

    def has_open_prs(self, sha):
        pulls = self.get(f"commits/{sha}/pulls")
        if pulls:
            return any((pull['state'] == 'open' for pull in pulls))
        return True

    def getHead(self, pr):
        pull = self.get(f"pulls/{pr}")
        if pull:
            return pull.get("head", {}).get("sha")
        return None


class Checklist(object):
    def __init__(self, body=None):
        self.process(body or "")

    @staticmethod
    def format_line(item, check):
        status = ""
        if isinstance(check, str):
            status = check + ": "
            check = False
        return " * [{0}] {1}{2}".format(check and "x" or " ", status, item)

    @staticmethod
    def parse_line(line):
        check = item = None
        stripped = line.strip()
        if stripped[:6] in ["* [ ] ", "- [ ] ", "* [x] ", "- [x] ", "* [X] ", "- [X] "]:
            status, unused, item = stripped[6:].strip().partition(": ")
            if not item:
                item = status
                status = None
            if status:
                check = status
            else:
                check = stripped[3] in ["x", "X"]
        return (item, check)

    def process(self, body, items={}):
        self.items = {}
        lines = []
        items = items.copy()
        for line in body.splitlines():
            (item, check) = self.parse_line(line)
            if item:
                if item in items:
                    check = items[item]
                    del items[item]
                    line = self.format_line(item, check)
                self.items[item] = check
            lines.append(line)
        for item, check in items.items():
            lines.append(self.format_line(item, check))
            self.items[item] = check
        self.body = "\n".join(lines)

    def check(self, item, checked=True):
        self.process(self.body, {item: checked})

    def add(self, item):
        self.process(self.body, {item: False})

    def checked(self):
        result = {}
        for item, check in self.items.items():
            if check:
                result[item] = check
        return result
