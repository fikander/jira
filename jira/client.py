#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from __future__ import print_function

"""
This module implements a friendly (well, friendlier) interface between the raw JSON
responses from JIRA and the Resource/dict abstractions provided by this library. Users
will construct a JIRA object as described below. Full API documentation can be found
at: https://jira-python.readthedocs.org/en/latest/
"""

from functools import wraps

import imghdr
import mimetypes

import collections
import copy
import os
import re
import tempfile
import logging
try:  # Python 2.7+
    from logging import NullHandler
except ImportError:
    class NullHandler(logging.Handler):

        def emit(self, record):
            pass
import json
import warnings
import sys
import datetime
import calendar
import hashlib
import urllib
from numbers import Number

# noinspection PyUnresolvedReferences
from six.moves.urllib.parse import urlparse, urlencode
from six import iteritems
from requests.utils import get_netrc_auth

try:
    from collections import OrderedDict
except ImportError:
    # noinspection PyUnresolvedReferences
    from ordereddict import OrderedDict

from six import string_types, integer_types
# six.moves does not play well with pyinstaller, see https://github.com/pycontribs/jira/issues/38
# from six.moves import html_parser
if sys.version_info < (3, 0, 0):
    import HTMLParser as html_parser
else:
    import html.parser as html_parser
import requests
try:
    # noinspection PyUnresolvedReferences
    from requests_toolbelt import MultipartEncoder
except:
    pass

try:
    from requests_jwt import JWTAuth
except ImportError:
    pass

# JIRA specific resources
from .resources import Resource, Issue, Comment, Project, Attachment, Component, Dashboard, Filter, Votes, Watchers, \
    Worklog, IssueLink, IssueLinkType, IssueType, Priority, Version, Role, Resolution, SecurityLevel, Status, User, \
    UserFromKey, CustomFieldOption, RemoteLink
# GreenHopper specific resources
from .resources import GreenHopperResource, Board, Sprint
from .resilientsession import ResilientSession, raise_on_error
from .version import __version__
from .utils import threaded_requests, json_loads, CaseInsensitiveDict
from .exceptions import JIRAError
try:
    from random import SystemRandom

    random = SystemRandom()
except ImportError:
    import random

# warnings.simplefilter('default')

# encoding = sys.getdefaultencoding()
# if encoding != 'UTF8':
#    warnings.warning("Python default encoding is '%s' instead of 'UTF8' which means that there is a big change of having problems. Possible workaround http://stackoverflow.com/a/17628350/99834" % encoding)

logging.getLogger('jira').addHandler(NullHandler())


def translate_resource_args(func):
    """
    Decorator that converts Issue and Project resources to their keys when used as arguments.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        arg_list = []
        for arg in args:
            if isinstance(arg, (Issue, Project)):
                arg_list.append(arg.key)
            else:
                arg_list.append(arg)
        result = func(*arg_list, **kwargs)
        return result

    return wrapper


def _get_template_list(data):
    template_list = []
    if 'projectTemplates' in data:
        template_list = data['projectTemplates']
    elif 'projectTemplatesGroupedByType' in data:
        for group in data['projectTemplatesGroupedByType']:
            template_list.extend(group['projectTemplates'])
    return template_list


class ResultList(list):

    def __init__(self, iterable=None, _startAt=None, _maxResults=None, _total=None, _isLast=None):
        if iterable is not None:
            list.__init__(self, iterable)
        else:
            list.__init__(self)

        self.startAt = _startAt
        self.maxResults = _maxResults
        # Optional parameters:
        self.isLast = _isLast
        self.total = _total


class QshGenerator:

    def __init__(self, context_path):
        self.context_path = context_path

    def __call__(self, req):
        parse_result = urlparse(req.url)

        path = parse_result.path[len(self.context_path):] if len(self.context_path) > 1 else parse_result.path
        query = '&'.join(sorted(parse_result.query.split("&")))
        qsh = '%(method)s&%(path)s&%(query)s' % {'method': req.method.upper(), 'path': path, 'query': query}

        return hashlib.sha256(qsh).hexdigest()


class JIRA(object):
    """
    User interface to JIRA.

    Clients interact with JIRA by constructing an instance of this object and calling its methods. For addressable
    resources in JIRA -- those with "self" links -- an appropriate subclass of :py:class:`Resource` will be returned
    with customized ``update()`` and ``delete()`` methods, along with attribute access to fields. This means that calls
    of the form ``issue.fields.summary`` will be resolved into the proper lookups to return the JSON value at that
    mapping. Methods that do not return resources will return a dict constructed from the JSON response or a scalar
    value; see each method's documentation for details on what that method returns.
    """

    DEFAULT_OPTIONS = {
        "server": "http://localhost:2990/jira",
        "context_path": "/",
        "rest_path": "api",
        "rest_api_version": "2",
        "agile_rest_path": GreenHopperResource.GREENHOPPER_REST_PATH,
        "agile_rest_api_version": "1.0",
        "verify": True,
        "resilient": True,
        "async": False,
        "client_cert": None,
        "check_update": True,
        "headers": {
            'X-Atlassian-Token': 'no-check',
            'Cache-Control': 'no-cache',
            # 'Accept': 'application/json;charset=UTF-8',  # default for REST
            'Content-Type': 'application/json',  # ;charset=UTF-8',
            # 'Accept': 'application/json',  # default for REST

            # 'Pragma': 'no-cache',
            # 'Expires': 'Thu, 01 Jan 1970 00:00:00 GMT'
        }
    }

    checked_version = False

    # TODO: remove these two variables and use the ones defined in resources
    JIRA_BASE_URL = Resource.JIRA_BASE_URL
    AGILE_BASE_URL = GreenHopperResource.AGILE_BASE_URL
    JIRA_SOFTWARE_BASE_URL = '{server}/rest/agile/latest/{path}'

    def __init__(self, server=None, options=None, basic_auth=None, oauth=None, jwt=None,
                 validate=False, get_server_info=True, async=False, logging=True, max_retries=3):
        """
        Construct a JIRA client instance.

        Without any arguments, this client will connect anonymously to the JIRA instance
        started by the Atlassian Plugin SDK from one of the 'atlas-run', ``atlas-debug``,
        or ``atlas-run-standalone`` commands. By default, this instance runs at
        ``http://localhost:2990/jira``. The ``options`` argument can be used to set the JIRA instance to use.

        Authentication is handled with the ``basic_auth`` argument. If authentication is supplied (and is
        accepted by JIRA), the client will remember it for subsequent requests.

        For quick command line access to a server, see the ``jirashell`` script included with this distribution.

        The easiest way to instantiate is using j = JIRA("https://jira.atlasian.com")

        :param options: Specify the server and properties this client will use. Use a dict with any
            of the following properties:
            * server -- the server address and context path to use. Defaults to ``http://localhost:2990/jira``.
            * rest_path -- the root REST path to use. Defaults to ``api``, where the JIRA REST resources live.
            * rest_api_version -- the version of the REST resources under rest_path to use. Defaults to ``2``.
            * agile_rest_path - the REST path to use for JIRA Agile requests. Defaults to ``greenhopper`` (old, private
               API). Check `GreenHopperResource` for other supported values.
            * verify -- Verify SSL certs. Defaults to ``True``.
            * client_cert -- a tuple of (cert,key) for the requests library for client side SSL
            * check_update -- Check whether using the newest python-jira library version.
        :param basic_auth: A tuple of username and password to use when establishing a session via HTTP BASIC
        authentication.
        :param oauth: A dict of properties for OAuth authentication. The following properties are required:
            * access_token -- OAuth access token for the user
            * access_token_secret -- OAuth access token secret to sign with the key
            * consumer_key -- key of the OAuth application link defined in JIRA
            * key_cert -- private key file to sign requests with (should be the pair of the public key supplied to
            JIRA in the OAuth application link)
        :param jwt: A dict of properties for JWT authentication supported by Atlassian Connect. The following
            properties are required:
            * secret -- shared secret as delivered during 'installed' lifecycle event
            (see https://developer.atlassian.com/static/connect/docs/latest/modules/lifecycle.html for details)
            * payload -- dict of fields to be inserted in the JWT payload, e.g. 'iss'
            Example jwt structure: ``{'secret': SHARED_SECRET, 'payload': {'iss': PLUGIN_KEY}}``
        :param validate: If true it will validate your credentials first. Remember that if you are accesing JIRA
            as anononymous it will fail to instanciate.
        :param get_server_info: If true it will fetch server version info first to determine if some API calls
            are available.
        :param async: To enable async requests for those actions where we implemented it, like issue update() or delete().
        Obviously this means that you cannot rely on the return code when this is enabled.
        """
        if options is None:
            options = {}
            if server and hasattr(server, 'keys'):
                warnings.warn(
                    "Old API usage, use JIRA(url) or JIRA(options={'server': url}, when using dictionary always use named parameters.",
                    DeprecationWarning)
                options = server
                server = None

        if server:
            options['server'] = server
        if async:
            options['async'] = async

        self.logging = logging

        self._options = copy.copy(JIRA.DEFAULT_OPTIONS)

        self._options.update(options)

        self._rank = None

        # Rip off trailing slash since all urls depend on that
        if self._options['server'].endswith('/'):
            self._options['server'] = self._options['server'][:-1]

        context_path = urlparse(self._options['server']).path
        if len(context_path) > 0:
            self._options['context_path'] = context_path

        self._try_magic()

        if oauth:
            self._create_oauth_session(oauth)
        elif basic_auth:
            self._create_http_basic_session(*basic_auth)
            self._session.headers.update(self._options['headers'])
        elif jwt:
            self._create_jwt_session(jwt)
        else:
            verify = self._options['verify']
            self._session = ResilientSession()
            self._session.verify = verify
        self._session.headers.update(self._options['headers'])

        self._session.max_retries = max_retries

        if validate:
            # This will raise an Exception if you are not allowed to login.
            # It's better to fail faster than later.
            self.session()

        if get_server_info:
            # We need version in order to know what API calls are available or not
            si = self.server_info()
            try:
                self._version = tuple(si['versionNumbers'])
            except Exception as e:
                logging.error("invalid server_info: %s", si)
                raise e
        else:
            self._version = (0, 0, 0)

        if self._options['check_update'] and not JIRA.checked_version:
            self._check_update_()
            JIRA.checked_version = True

        # TODO: check if this works with non-admin accounts
        self._fields = {}
        for f in self.fields():
            if 'clauseNames' in f:
                for name in f['clauseNames']:
                    self._fields[name] = f['id']

    def _check_update_(self):
        # check if the current version of the library is outdated
        try:
            data = requests.get("http://pypi.python.org/pypi/jira/json", timeout=2.001).json()

            released_version = data['info']['version']
            if released_version > __version__:
                warnings.warn(
                    "You are running an outdated version of JIRA Python %s. Current version is %s. Do not file any bugs against older versions." % (
                        __version__, released_version))
        except requests.RequestException:
            pass
        except Exception as e:
            logging.warning(e)

    def __del__(self):
        session = getattr(self, "_session", None)
        if session is not None:
            if sys.version_info < (3, 4, 0):  # workaround for https://github.com/kennethreitz/requests/issues/2303
                session.close()

    def _check_for_html_error(self, content):
        # TODO: Make it return errors when content is a webpage with errors
        # JIRA has the bad habbit of returning errors in pages with 200 and
        # embedding the error in a huge webpage.
        if '<!-- SecurityTokenMissing -->' in content:
            logging.warning("Got SecurityTokenMissing")
            raise JIRAError("SecurityTokenMissing: %s" % content)
            return False
        return True

    def _fetch_pages(self, item_type, items_key, request_path, startAt=0, maxResults=50, params=None, base=JIRA_BASE_URL):
        """
        Fetches
        :param item_type: Type of single item. ResultList of such items will be returned.
        :param items_key: Path to the items in JSON returned from server.
                Set it to None, if response is an array, and not a JSON object.
        :param request_path: path in request URL
        :param startAt: index of the first record to be fetched
        :param maxResults: Maximum number of items to return.
                If maxResults evaluates as False, it will try to get all items in batches.
        :param params: Params to be used in all requests. Should not contain startAt and maxResults,
                        as they will be added for each request created from this function.
        :param base: base URL
        :return: ResultList
        """

        page_params = params.copy() if params else {}
        if startAt:
            page_params['startAt'] = startAt
        if maxResults:
            page_params['maxResults'] = maxResults
        resource = self._get_json(request_path, params=page_params, base=base)
        next_items_page = [item_type(self._options, self._session, raw_issue_json) for raw_issue_json in
                           (resource[items_key] if items_key else resource)]
        items = next_items_page

        if True:  # isinstance(resource, dict):

            if isinstance(resource, dict):
                total = resource.get('total', 1)
                # 'isLast' is the optional key added to responses in JIRA Agile 6.7.6. So far not used in basic JIRA API.
                is_last = resource.get('isLast', True)
                start_at_from_response = resource.get('startAt', 0)
                max_results_from_response = resource.get('maxResults', 1)
            else:
                # if is a list
                total = 1
                is_last = True
                start_at_from_response = 0
                max_results_from_response = 1

            # If maxResults evaluates as False, get all items in batches
            if not maxResults:
                page_size = max_results_from_response or len(items)
                page_start = (startAt or start_at_from_response or 0) + page_size
                while not is_last and (total is None or page_start < total) and len(next_items_page) == page_size:
                    page_params['startAt'] = page_start
                    page_params['maxResults'] = page_size
                    resource = self._get_json(request_path, params=page_params, base=base)
                    next_items_page = [item_type(self._options, self._session, raw_issue_json) for raw_issue_json in
                                       (resource[items_key] if items_key else resource)]
                    items.extend(next_items_page)
                    page_start += page_size

            return ResultList(items, start_at_from_response, max_results_from_response, total, is_last)
        else:
            # it seams that search_users can return a list() containing a single user!
            return ResultList([item_type(self._options, self._session, resource)], 0, 1, 1, True)

    # Information about this client

    def client_info(self):
        """Get the server this client is connected to."""
        return self._options['server']

    # Universal resource loading

    def find(self, resource_format, ids=None):
        """
        Get a Resource object for any addressable resource on the server.

        This method is a universal resource locator for any RESTful resource in JIRA. The
        argument ``resource_format`` is a string of the form ``resource``, ``resource/{0}``,
        ``resource/{0}/sub``, ``resource/{0}/sub/{1}``, etc. The format placeholders will be
        populated from the ``ids`` argument if present. The existing authentication session
        will be used.

        The return value is an untyped Resource object, which will not support specialized
        :py:meth:`.Resource.update` or :py:meth:`.Resource.delete` behavior. Moreover, it will
        not know to return an issue Resource if the client uses the resource issue path. For this
        reason, it is intended to support resources that are not included in the standard
        Atlassian REST API.

        :param resource_format: the subpath to the resource string
        :param ids: values to substitute in the ``resource_format`` string
        :type ids: tuple or None
        """
        resource = Resource(resource_format, self._options, self._session)
        resource.find(ids)
        return resource

    def async_do(self, size=10):
        """
        This will execute all async jobs and wait for them to finish. By default it will run on 10 threads.

        size: number of threads to run on.
        :return:
        """
        if hasattr(self._session, '_async_jobs'):
            logging.info("Executing async %s jobs found in queue by using %s threads..." % (
                len(self._session._async_jobs), size))
            threaded_requests.map(self._session._async_jobs, size=size)

            # Application properties

    # non-resource
    def application_properties(self, key=None):
        """
        Return the mutable server application properties.

        :param key: the single property to return a value for
        """
        params = {}
        if key is not None:
            params['key'] = key
        return self._get_json('application-properties', params=params)

    def set_application_property(self, key, value):
        """
        Set the application property.

        :param key: key of the property to set
        :param value: value to assign to the property
        """
        url = self._options['server'] + \
            '/rest/api/latest/application-properties/' + key
        payload = {
            'id': key,
            'value': value
        }
        r = self._session.put(
            url, data=json.dumps(payload))

    def applicationlinks(self, cached=True):
        """
        List of application links
        :return: json
        """

        # if cached, return the last result
        if cached and hasattr(self, '_applicationlinks'):
            return self._applicationlinks

        # url = self._options['server'] + '/rest/applinks/latest/applicationlink'
        url = self._options['server'] + \
            '/rest/applinks/latest/listApplicationlinks'

        r = self._session.get(url)

        o = json_loads(r)
        if 'list' in o:
            self._applicationlinks = o['list']
        else:
            self._applicationlinks = []
        return self._applicationlinks

    # Attachments
    def attachment(self, id):
        """Get an attachment Resource from the server for the specified ID."""
        return self._find_for_resource(Attachment, id)

    # non-resource
    def attachment_meta(self):
        """Get the attachment metadata."""
        return self._get_json('attachment/meta')

    @translate_resource_args
    def add_attachment(self, issue, attachment, filename=None):
        """
        Attach an attachment to an issue and returns a Resource for it.

        The client will *not* attempt to open or validate the attachment; it expects a file-like object to be ready
        for its use. The user is still responsible for tidying up (e.g., closing the file, killing the socket, etc.)

        :param issue: the issue to attach the attachment to
        :param attachment: file-like object to attach to the issue, also works if it is a string with the filename.
        :param filename: optional name for the attached file. If omitted, the file object's ``name`` attribute
            is used. If you aquired the file-like object by any other method than ``open()``, make sure
            that a name is specified in one way or the other.
        :rtype: an Attachment Resource
        """
        if isinstance(attachment, string_types):
            attachment = open(attachment, "rb")
        if hasattr(attachment, 'read') and hasattr(attachment, 'mode') and attachment.mode != 'rb':
            logging.warning(
                "%s was not opened in 'rb' mode, attaching file may fail." % attachment.name)

        # TODO: Support attaching multiple files at once?
        url = self._get_url('issue/' + str(issue) + '/attachments')

        fname = filename
        if not fname:
            fname = os.path.basename(attachment.name)

        if 'MultipartEncoder' not in globals():
            method = 'old'
            r = self._session.post(
                url,
                files={
                    'file': (fname, attachment, 'application/octet-stream')},
                headers=CaseInsensitiveDict({'content-type': None, 'X-Atlassian-Token': 'nocheck'}))
        else:
            method = 'MultipartEncoder'

            def file_stream():
                return MultipartEncoder(
                    fields={
                        'file': (fname, attachment, 'application/octet-stream')}
                )
            m = file_stream()
            r = self._session.post(
                url, data=m, headers=CaseInsensitiveDict({'content-type': m.content_type, 'X-Atlassian-Token': 'nocheck'}), retry_data=file_stream)

        js = json_loads(r)
        if not js or not isinstance(js, collections.Iterable):
            raise JIRAError("Unable to parse JSON: %s" % js)
        attachment = Attachment(self._options, self._session, js[0])
        if attachment.size == 0:
            raise JIRAError("Added empty attachment via %s method?!: r: %s\nattachment: %s" % (method, r, attachment))
        return attachment

    # Components

    def component(self, id):
        """
        Get a component Resource from the server.

        :param id: ID of the component to get
        """
        return self._find_for_resource(Component, id)

    @translate_resource_args
    def create_component(self, name, project, description=None, leadUserName=None, assigneeType=None,
                         isAssigneeTypeValid=False):
        """
        Create a component inside a project and return a Resource for it.

        :param name: name of the component
        :param project: key of the project to create the component in
        :param description: a description of the component
        :param leadUserName: the username of the user responsible for this component
        :param assigneeType: see the ComponentBean.AssigneeType class for valid values
        :param isAssigneeTypeValid: boolean specifying whether the assignee type is acceptable
        """
        data = {
            'name': name,
            'project': project,
            'isAssigneeTypeValid': isAssigneeTypeValid
        }
        if description is not None:
            data['description'] = description
        if leadUserName is not None:
            data['leadUserName'] = leadUserName
        if assigneeType is not None:
            data['assigneeType'] = assigneeType

        url = self._get_url('component')
        r = self._session.post(
            url, data=json.dumps(data))

        component = Component(self._options, self._session, raw=json_loads(r))
        return component

    def component_count_related_issues(self, id):
        """
        Get the count of related issues for a component.

        :type id: integer
        :param id: ID of the component to use
        """
        return self._get_json('component/' + id + '/relatedIssueCounts')['issueCount']

    # Custom field options

    def custom_field_option(self, id):
        """
        Get a custom field option Resource from the server.

        :param id: ID of the custom field to use
        """
        return self._find_for_resource(CustomFieldOption, id)

    # Dashboards

    def dashboards(self, filter=None, startAt=0, maxResults=20):
        """
        Return a ResultList of Dashboard resources and a ``total`` count.

        :param filter: either "favourite" or "my", the type of dashboards to return
        :param startAt: index of the first dashboard to return
        :param maxResults: maximum number of dashboards to return.
            If maxResults evaluates as False, it will try to get all items in batches.

        :rtype ResultList
        """
        params = {}
        if filter is not None:
            params['filter'] = filter
        return self._fetch_pages(Dashboard, 'dashboards', 'dashboard', startAt, maxResults, params)

    def dashboard(self, id):
        """
        Get a dashboard Resource from the server.

        :param id: ID of the dashboard to get.
        """
        return self._find_for_resource(Dashboard, id)

    # Fields

    # non-resource
    def fields(self):
        """Return a list of all issue fields."""
        return self._get_json('field')

    # Filters

    def filter(self, id):
        """
        Get a filter Resource from the server.

        :param id: ID of the filter to get.
        """
        return self._find_for_resource(Filter, id)

    def favourite_filters(self):
        """Get a list of filter Resources which are the favourites of the currently authenticated user."""
        r_json = self._get_json('filter/favourite')
        filters = [Filter(self._options, self._session, raw_filter_json)
                   for raw_filter_json in r_json]
        return filters

    def create_filter(self, name=None, description=None,
                      jql=None, favourite=None):
        """
        Create a new filter and return a filter Resource for it.

        Keyword arguments:
        name -- name of the new filter
        description -- useful human readable description of the new filter
        jql -- query string that defines the filter
        favourite -- whether to add this filter to the current user's favorites

        """
        data = {}
        if name is not None:
            data['name'] = name
        if description is not None:
            data['description'] = description
        if jql is not None:
            data['jql'] = jql
        if favourite is not None:
            data['favourite'] = favourite
        url = self._get_url('filter')
        r = self._session.post(
            url, data=json.dumps(data))

        raw_filter_json = json_loads(r)
        return Filter(self._options, self._session, raw=raw_filter_json)

    def update_filter(self, filter_id,
                      name=None, description=None,
                      jql=None, favourite=None):
        """
        Updates a filter and return a filter Resource for it.

        Keyword arguments:
        name -- name of the new filter
        description -- useful human readable description of the new filter
        jql -- query string that defines the filter
        favourite -- whether to add this filter to the current user's favorites

        """
        filter = self.filter(filter_id)
        data = {}
        data['name'] = name or filter.name
        data['description'] = description or filter.description
        data['jql'] = jql or filter.jql
        data['favourite'] = favourite or filter.favourite

        url = self._get_url('filter/%s' % filter_id)
        r = self._session.put(url, headers={'content-type': 'application/json'},
                              data=json.dumps(data))

        raw_filter_json = json.loads(r.text)
        return Filter(self._options, self._session, raw=raw_filter_json)

# Groups

    # non-resource
    def groups(self, query=None, exclude=None, maxResults=9999):
        """
        Return a list of groups matching the specified criteria.

        :param query: filter groups by name with this string
        :param exclude: filter out groups by name with this string
        :param maxResults: maximum results to return. defaults to 9999
        """
        params = {}
        groups = []
        if query is not None:
            params['query'] = query
        if exclude is not None:
            params['exclude'] = exclude
        if maxResults is not None:
            params['maxResults'] = maxResults
        for group in self._get_json('groups/picker', params=params)['groups']:
            groups.append(group['name'])
        return sorted(groups)

    def group_members(self, group):
        """
        Return a hash or users with their information. Requires JIRA 6.0 or will raise NotImplemented.
        """
        if self._version < (6, 0, 0):
            raise NotImplementedError(
                "Group members is not implemented in JIRA before version 6.0, upgrade the instance, if possible.")

        params = {'groupname': group, 'expand': "users"}
        r = self._get_json('group', params=params)
        size = r['users']['size']
        end_index = r['users']['end-index']

        while end_index < size - 1:
            params = {'groupname': group, 'expand': "users[%s:%s]" % (
                end_index + 1, end_index + 50)}
            r2 = self._get_json('group', params=params)
            for user in r2['users']['items']:
                r['users']['items'].append(user)
            end_index = r2['users']['end-index']
            size = r['users']['size']

        result = {}
        for user in r['users']['items']:
            result[user['name']] = {'fullname': user['displayName'], 'email': user['emailAddress'],
                                    'active': user['active']}
        return result

    def add_group(self, groupname):
        '''
        Creates a new group in JIRA.
        :param groupname: The name of the group you wish to create.
        :return: Boolean - True if succesfull.
        '''
        url = self._options['server'] + '/rest/api/latest/group'

        # implementation based on
        # https://docs.atlassian.com/jira/REST/ondemand/#d2e5173

        x = OrderedDict()

        x['name'] = groupname

        payload = json.dumps(x)

        self._session.post(url, data=payload)

        return True

    def remove_group(self, groupname):
        '''
        Deletes a group from the JIRA instance.
        :param groupname: The group to be deleted from the JIRA instance.
        :return: Boolean. Returns True on success.
        '''

        # implementation based on
        # https://docs.atlassian.com/jira/REST/ondemand/#d2e5173
        url = self._options['server'] + '/rest/api/latest/group'
        x = {'groupname': groupname}
        self._session.delete(url, params=x)
        return True

    # Issues

    def issue(self, id, fields=None, expand=None):
        """
        Get an issue Resource from the server.

        :param id: ID or key of the issue to get
        :param fields: comma-separated string of issue fields to include in the results
        :param expand: extra information to fetch inside each resource
        """

        # this allows us to pass Issue objects to issue()
        if type(id) == Issue:
            return id

        issue = Issue(self._options, self._session)

        params = {}
        if fields is not None:
            params['fields'] = fields
        if expand is not None:
            params['expand'] = expand
        issue.find(id, params=params)
        return issue

    def create_issue(self, fields=None, prefetch=True, **fieldargs):
        """
        Create a new issue and return an issue Resource for it.

        Each keyword argument (other than the predefined ones) is treated as a field name and the argument's value
        is treated as the intended value for that field -- if the fields argument is used, all other keyword arguments
        will be ignored.

        By default, the client will immediately reload the issue Resource created by this method in order to return
        a complete Issue object to the caller; this behavior can be controlled through the 'prefetch' argument.

        JIRA projects may contain many different issue types. Some issue screens have different requirements for
        fields in a new issue. This information is available through the 'createmeta' method. Further examples are
        available here: https://developer.atlassian.com/display/JIRADEV/JIRA+REST+API+Example+-+Create+Issue

        :param fields: a dict containing field names and the values to use. If present, all other keyword arguments\
        will be ignored
        :param prefetch: whether to reload the created issue Resource so that all of its data is present in the value\
        returned from this method
        """
        data = {}
        if fields is not None:
            data['fields'] = fields
        else:
            fields_dict = {}
            for field in fieldargs:
                fields_dict[field] = fieldargs[field]
            data['fields'] = fields_dict

        p = data['fields']['project']
        if isinstance(p, string_types) or isinstance(p, integer_types):
            data['fields']['project'] = {'id': self.project(p).id}

        p = data['fields']['issuetype']
        if isinstance(p, integer_types):
            data['fields']['issuetype'] = {'id': p}
        if isinstance(p, string_types) or isinstance(p, integer_types):
            data['fields']['issuetype'] = {'id': self.issue_type_by_name(p).id}

        url = self._get_url('issue')
        r = self._session.post(url, data=json.dumps(data))

        raw_issue_json = json_loads(r)
        if 'key' not in raw_issue_json:
            raise JIRAError(r.status_code, request=r)
        if prefetch:
            return self.issue(raw_issue_json['key'])
        else:
            return Issue(self._options, self._session, raw=raw_issue_json)

    def createmeta(self, projectKeys=None, projectIds=[], issuetypeIds=None, issuetypeNames=None, expand=None):
        """
        Gets the metadata required to create issues, optionally filtered by projects and issue types.

        :param projectKeys: keys of the projects to filter the results with. Can be a single value or a comma-delimited\
        string. May be combined with projectIds.
        :param projectIds: IDs of the projects to filter the results with. Can be a single value or a comma-delimited\
        string. May be combined with projectKeys.
        :param issuetypeIds: IDs of the issue types to filter the results with. Can be a single value or a\
        comma-delimited string. May be combined with issuetypeNames.
        :param issuetypeNames: Names of the issue types to filter the results with. Can be a single value or a\
        comma-delimited string. May be combined with issuetypeIds.
        :param expand: extra information to fetch inside each resource.
        """
        params = {}
        if projectKeys is not None:
            params['projectKeys'] = projectKeys
        if projectIds is not None:
            if isinstance(projectIds, string_types):
                projectIds = projectIds.split(',')
            params['projectIds'] = projectIds
        if issuetypeIds is not None:
            params['issuetypeIds'] = issuetypeIds
        if issuetypeNames is not None:
            params['issuetypeNames'] = issuetypeNames
        if expand is not None:
            params['expand'] = expand
        return self._get_json('issue/createmeta', params)

    # non-resource
    @translate_resource_args
    def assign_issue(self, issue, assignee):
        """
        Assign an issue to a user. None will set it to unassigned. -1 will set it to Automatic.

        :param issue: the issue to assign
        :param assignee: the user to assign the issue to
        """
        url = self._options['server'] + \
            '/rest/api/latest/issue/' + str(issue) + '/assignee'
        payload = {'name': assignee}
        r = self._session.put(
            url, data=json.dumps(payload))
        raise_on_error(r)
        return True

    @translate_resource_args
    def comments(self, issue):
        """
        Get a list of comment Resources.

        :param issue: the issue to get comments from
        """
        r_json = self._get_json('issue/' + str(issue) + '/comment')

        comments = [Comment(self._options, self._session, raw_comment_json)
                    for raw_comment_json in r_json['comments']]
        return comments

    @translate_resource_args
    def comment(self, issue, comment):
        """
        Get a comment Resource from the server for the specified ID.

        :param issue: ID or key of the issue to get the comment from
        :param comment: ID of the comment to get
        """
        return self._find_for_resource(Comment, (issue, comment))

    @translate_resource_args
    def add_comment(self, issue, body, visibility=None):
        """
        Add a comment from the current authenticated user on the specified issue and return a Resource for it.
        The issue identifier and comment body are required.

        :param issue: ID or key of the issue to add the comment to
        :param body: Text of the comment to add
        :param visibility: a dict containing two entries: "type" and "value". "type" is 'role' (or 'group' if the JIRA\
        server has configured comment visibility for groups) and 'value' is the name of the role (or group) to which\
        viewing of this comment will be restricted.
        """
        data = {
            'body': body
        }
        if visibility is not None:
            data['visibility'] = visibility

        url = self._get_url('issue/' + str(issue) + '/comment')
        r = self._session.post(
            url, data=json.dumps(data))

        comment = Comment(self._options, self._session, raw=json_loads(r))
        return comment

    # non-resource
    @translate_resource_args
    def editmeta(self, issue):
        """
        Get the edit metadata for an issue.

        :param issue: the issue to get metadata for
        """
        return self._get_json('issue/' + str(issue) + '/editmeta')

    @translate_resource_args
    def remote_links(self, issue):
        """
        Get a list of remote link Resources from an issue.

        :param issue: the issue to get remote links from
        """
        r_json = self._get_json('issue/' + str(issue) + '/remotelink')
        remote_links = [RemoteLink(
            self._options, self._session, raw_remotelink_json) for raw_remotelink_json in r_json]
        return remote_links

    @translate_resource_args
    def remote_link(self, issue, id):
        """
        Get a remote link Resource from the server.

        :param issue: the issue holding the remote link
        :param id: ID of the remote link
        """
        return self._find_for_resource(RemoteLink, (issue, id))

    # removed the @translate_resource_args because it prevents us from finding
    # information for building a proper link
    def add_remote_link(self, issue, destination, globalId=None, application=None, relationship=None):
        """
        Add a remote link from an issue to an external application and returns a remote link Resource
        for it. ``object`` should be a dict containing at least ``url`` to the linked external URL and
        ``title`` to display for the link inside JIRA.

        For definitions of the allowable fields for ``object`` and the keyword arguments ``globalId``, ``application``
        and ``relationship``, see https://developer.atlassian.com/display/JIRADEV/JIRA+REST+API+for+Remote+Issue+Links.

        :param issue: the issue to add the remote link to
        :param destination: the link details to add (see the above link for details)
        :param globalId: unique ID for the link (see the above link for details)
        :param application: application information for the link (see the above link for details)
        :param relationship: relationship description for the link (see the above link for details)
        """

        try:
            applicationlinks = self.applicationlinks()
        except JIRAError as e:
            applicationlinks = []
            # In many (if not most) configurations, non-admin users are
            # not allowed to list applicationlinks; if we aren't allowed,
            # let's let people try to add remote links anyway, we just
            # won't be able to be quite as helpful.
            warnings.warn(
                "Unable to gather applicationlinks; you will not be able "
                "to add links to remote issues: (%s) %s" % (
                    e.status_code,
                    e.text
                ),
                Warning
            )

        data = {}
        if type(destination) == Issue:

            data['object'] = {
                'title': str(destination),
                'url': destination.permalink()
            }

            for x in applicationlinks:
                if x['application']['displayUrl'] == destination._options['server']:
                    data['globalId'] = "appId=%s&issueId=%s" % (
                        x['application']['id'], destination.raw['id'])
                    data['application'] = {
                        'name': x['application']['name'], 'type': "com.atlassian.jira"}
                    break
            if 'globalId' not in data:
                raise NotImplementedError(
                    "Unable to identify the issue to link to.")
        else:

            if globalId is not None:
                data['globalId'] = globalId
            if application is not None:
                data['application'] = application
            data['object'] = destination

        if relationship is not None:
            data['relationship'] = relationship

        # check if the link comes from one of the configured application links
        for x in applicationlinks:
            if x['application']['displayUrl'] == self._options['server']:
                data['globalId'] = "appId=%s&issueId=%s" % (
                    x['application']['id'], destination.raw['id'])
                data['application'] = {
                    'name': x['application']['name'], 'type': "com.atlassian.jira"}
                break

        url = self._get_url('issue/' + str(issue) + '/remotelink')
        r = self._session.post(
            url, data=json.dumps(data))

        remote_link = RemoteLink(
            self._options, self._session, raw=json_loads(r))
        return remote_link

    def add_simple_link(self, issue, object):
        """
        Add a simple remote link from an issue to web resource.  This avoids the admin access problems from add_remote_link by just using a simple object and presuming all fields are correct and not requiring more complex ``application`` data.
            ``object`` should be a dict containing at least ``url`` to the linked external URL
             and ``title`` to display for the link inside JIRA.

        For definitions of the allowable fields for ``object`` , see https://developer.atlassian.com/display/JIRADEV/JIRA+REST+API+for+Remote+Issue+Links.

        :param issue: the issue to add the remote link to
        :param object: the dictionary used to create remotelink data
        """

        data = {}

        # hard code data dict to be passed as ``object`` to avoid any permissions errors
        data = object

        url = self._get_url('issue/' + str(issue) + '/remotelink')
        r = self._session.post(
            url, data=json.dumps(data))

        simple_link = RemoteLink(
            self._options, self._session, raw=json_loads(r))
        return simple_link

    # non-resource
    @translate_resource_args
    def transitions(self, issue, id=None, expand=None):
        """
        Get a list of the transitions available on the specified issue to the current user.

        :param issue: ID or key of the issue to get the transitions from
        :param id: if present, get only the transition matching this ID
        :param expand: extra information to fetch inside each transition
        """
        params = {}
        if id is not None:
            params['transitionId'] = id
        if expand is not None:
            params['expand'] = expand
        return self._get_json('issue/' + str(issue) + '/transitions', params=params)['transitions']

    def find_transitionid_by_name(self, issue, transition_name):
        """
        Get a transitionid available on the specified issue to the current user.
        Look at https://developer.atlassian.com/static/rest/jira/6.1.html#d2e1074 for json reference

        :param issue: ID or key of the issue to get the transitions from
        :param trans_name: iname of transition we are looking for
        """
        transitions_json = self.transitions(issue)
        id = None

        for transition in transitions_json:
            if transition["name"].lower() == transition_name.lower():
                id = transition["id"]
                break
        return id

    @translate_resource_args
    def transition_issue(self, issue, transition, fields=None, comment=None, **fieldargs):
        # TODO: Support update verbs (same as issue.update())
        """
        Perform a transition on an issue.

        Each keyword argument (other than the predefined ones) is treated as a field name and the argument's value
        is treated as the intended value for that field -- if the fields argument is used, all other keyword arguments
        will be ignored. Field values will be set on the issue as part of the transition process.

        :param issue: ID or key of the issue to perform the transition on
        :param transition: ID or name of the transition to perform
        :param comment: *Optional* String to add as comment to the issue when performing the transition.
        :param fields: a dict containing field names and the values to use. If present, all other keyword arguments\
        will be ignored
        """

        transitionId = None

        try:
            transitionId = int(transition)
        except:
            # cannot cast to int, so try to find transitionId by name
            transitionId = self.find_transitionid_by_name(issue, transition)
            if transitionId is None:
                raise JIRAError("Invalid transition name. %s" % transition)

        data = {
            'transition': {
                'id': transitionId
            }
        }
        if comment:
            data['update'] = {'comment': [{'add': {'body': comment}}]}
        if fields is not None:
            data['fields'] = fields
        else:
            fields_dict = {}
            for field in fieldargs:
                fields_dict[field] = fieldargs[field]
            data['fields'] = fields_dict

        url = self._get_url('issue/' + str(issue) + '/transitions')
        r = self._session.post(
            url, data=json.dumps(data))
        try:
            r_json = json_loads(r)
        except ValueError as e:
            logging.error("%s\n%s" % (e, r.text))
            raise e
        return r_json

    @translate_resource_args
    def votes(self, issue):
        """
        Get a votes Resource from the server.

        :param issue: ID or key of the issue to get the votes for
        """
        return self._find_for_resource(Votes, issue)

    @translate_resource_args
    def add_vote(self, issue):
        """
        Register a vote for the current authenticated user on an issue.

        :param issue: ID or key of the issue to vote on
        """
        url = self._get_url('issue/' + str(issue) + '/votes')
        r = self._session.post(url)

    @translate_resource_args
    def remove_vote(self, issue):
        """
        Remove the current authenticated user's vote from an issue.

        :param issue: ID or key of the issue to unvote on
        """
        url = self._get_url('issue/' + str(issue) + '/votes')
        self._session.delete(url)

    @translate_resource_args
    def watchers(self, issue):
        """
        Get a watchers Resource from the server for an issue.

        :param issue: ID or key of the issue to get the watchers for
        """
        return self._find_for_resource(Watchers, issue)

    @translate_resource_args
    def add_watcher(self, issue, watcher):
        """
        Add a user to an issue's watchers list.

        :param issue: ID or key of the issue affected
        :param watcher: username of the user to add to the watchers list
        """
        url = self._get_url('issue/' + str(issue) + '/watchers')
        self._session.post(
            url, data=json.dumps(watcher))

    @translate_resource_args
    def remove_watcher(self, issue, watcher):
        """
        Remove a user from an issue's watch list.

        :param issue: ID or key of the issue affected
        :param watcher: username of the user to remove from the watchers list
        """
        url = self._get_url('issue/' + str(issue) + '/watchers')
        params = {'username': watcher}
        result = self._session.delete(url, params=params)
        return result

    @translate_resource_args
    def worklogs(self, issue):
        """
        Get a list of worklog Resources from the server for an issue.

        :param issue: ID or key of the issue to get worklogs from
        """
        r_json = self._get_json('issue/' + str(issue) + '/worklog')
        worklogs = [Worklog(self._options, self._session, raw_worklog_json)
                    for raw_worklog_json in r_json['worklogs']]
        return worklogs

    @translate_resource_args
    def worklog(self, issue, id):
        """
        Get a specific worklog Resource from the server.

        :param issue: ID or key of the issue to get the worklog from
        :param id: ID of the worklog to get
        """
        return self._find_for_resource(Worklog, (issue, id))

    @translate_resource_args
    def add_worklog(self, issue, timeSpent=None, timeSpentSeconds=None, adjustEstimate=None,
                    newEstimate=None, reduceBy=None, comment=None, started=None, user=None):
        """
        Add a new worklog entry on an issue and return a Resource for it.

        :param issue: the issue to add the worklog to
        :param timeSpent: a worklog entry with this amount of time spent, e.g. "2d"
        :param adjustEstimate: (optional) allows the user to provide specific instructions to update the remaining\
        time estimate of the issue. The value can either be ``new``, ``leave``, ``manual`` or ``auto`` (default).
        :param newEstimate: the new value for the remaining estimate field. e.g. "2d"
        :param reduceBy: the amount to reduce the remaining estimate by e.g. "2d"
        :param started: Moment when the work is logged, if not specified will default to now
        :param comment: optional worklog comment
        """
        params = {}
        if adjustEstimate is not None:
            params['adjustEstimate'] = adjustEstimate
        if newEstimate is not None:
            params['newEstimate'] = newEstimate
        if reduceBy is not None:
            params['reduceBy'] = reduceBy

        data = {}
        if timeSpent is not None:
            data['timeSpent'] = timeSpent
        if timeSpentSeconds is not None:
            data['timeSpentSeconds'] = timeSpentSeconds
        if comment is not None:
            data['comment'] = comment
        elif user:
            # we log user inside comment as it doesn't always work
            data['comment'] = user

        if started is not None:
            # based on REST Browser it needs: "2014-06-03T08:21:01.273+0000"
            data['started'] = started.strftime("%Y-%m-%dT%H:%M:%S.000%z")
        if user is not None:
            data['author'] = {"name": user,
                              'self': self.JIRA_BASE_URL + '/rest/api/latest/user?username=' + user,
                              'displayName': user,
                              'active': False
                              }
            data['updateAuthor'] = data['author']
        # TODO: report bug to Atlassian: author and updateAuthor parameters are
        # ignored.
        url = self._get_url('issue/{0}/worklog'.format(issue))
        r = self._session.post(url, params=params, data=json.dumps(data))

        return Worklog(self._options, self._session, json_loads(r))

    # Issue links

    @translate_resource_args
    def create_issue_link(self, type, inwardIssue, outwardIssue, comment=None):
        """
        Create a link between two issues.

        :param type: the type of link to create
        :param inwardIssue: the issue to link from
        :param outwardIssue: the issue to link to
        :param comment:  a comment to add to the issues with the link. Should be a dict containing ``body``\
        and ``visibility`` fields: ``body`` being the text of the comment and ``visibility`` being a dict containing\
        two entries: ``type`` and ``value``. ``type`` is ``role`` (or ``group`` if the JIRA server has configured\
        comment visibility for groups) and ``value`` is the name of the role (or group) to which viewing of this\
        comment will be restricted.
        """

        # let's see if we have the right issue link 'type' and fix it if needed
        if not hasattr(self, '_cached_issuetypes'):
            self._cached_issue_link_types = self.issue_link_types()

        if type not in self._cached_issue_link_types:
            for lt in self._cached_issue_link_types:
                if lt.outward == type:
                    # we are smart to figure it out what he ment
                    type = lt.name
                    break
                elif lt.inward == type:
                    # so that's the reverse, so we fix the request
                    type = lt.name
                    inwardIssue, outwardIssue = outwardIssue, inwardIssue
                    break

        data = {
            'type': {
                'name': type
            },
            'inwardIssue': {
                'key': inwardIssue
            },
            'outwardIssue': {
                'key': outwardIssue
            },
            'comment': comment
        }
        url = self._get_url('issueLink')
        r = self._session.post(
            url, data=json.dumps(data))

    def issue_link(self, id):
        """
        Get an issue link Resource from the server.

        :param id: ID of the issue link to get
        """
        return self._find_for_resource(IssueLink, id)

    # Issue link types

    def issue_link_types(self):
        """Get a list of issue link type Resources from the server."""
        r_json = self._get_json('issueLinkType')
        link_types = [IssueLinkType(self._options, self._session, raw_link_json) for raw_link_json in
                      r_json['issueLinkTypes']]
        return link_types

    def issue_link_type(self, id):
        """
        Get an issue link type Resource from the server.

        :param id: ID of the issue link type to get
        """
        return self._find_for_resource(IssueLinkType, id)

    # Issue types

    def issue_types(self):
        """Get a list of issue type Resources from the server."""
        r_json = self._get_json('issuetype')
        issue_types = [IssueType(
            self._options, self._session, raw_type_json) for raw_type_json in r_json]
        return issue_types

    def issue_type(self, id):
        """
        Get an issue type Resource from the server.

        :param id: ID of the issue type to get
        """
        return self._find_for_resource(IssueType, id)

    def issue_type_by_name(self, name):
        issue_types = self.issue_types()
        try:
            issue_type = [it for it in issue_types if it.name == name][0]
        except IndexError:
            raise KeyError("Issue type '%s' is unknown." % name)
        return issue_type

    # User permissions

    # non-resource
    def my_permissions(self, projectKey=None, projectId=None, issueKey=None, issueId=None):
        """
        Get a dict of all available permissions on the server.

        :param projectKey: limit returned permissions to the specified project
        :param projectId: limit returned permissions to the specified project
        :param issueKey: limit returned permissions to the specified issue
        :param issueId: limit returned permissions to the specified issue
        """
        params = {}
        if projectKey is not None:
            params['projectKey'] = projectKey
        if projectId is not None:
            params['projectId'] = projectId
        if issueKey is not None:
            params['issueKey'] = issueKey
        if issueId is not None:
            params['issueId'] = issueId
        return self._get_json('mypermissions', params=params)

    # Priorities

    def priorities(self):
        """Get a list of priority Resources from the server."""
        r_json = self._get_json('priority')
        priorities = [Priority(
            self._options, self._session, raw_priority_json) for raw_priority_json in r_json]
        return priorities

    def priority(self, id):
        """
        Get a priority Resource from the server.

        :param id: ID of the priority to get
        """
        return self._find_for_resource(Priority, id)

    # Projects

    def projects(self):
        """Get a list of project Resources from the server visible to the current authenticated user."""
        r_json = self._get_json('project')
        projects = [Project(
            self._options, self._session, raw_project_json) for raw_project_json in r_json]
        return projects

    def project(self, id):
        """
        Get a project Resource from the server.

        :param id: ID or key of the project to get
        """
        return self._find_for_resource(Project, id)

    @translate_resource_args
    def project_statuses(self, project):
        """
        Get all issue types with valid status values for a project.

        :param id: ID or key of the project to get statuses for
        """
        r_json = self._get_json('project/' + project + '/statuses')
        statuses = []
        status_ids = {}
        for raw_issuetype_json in r_json:
            for raw_stat_json in raw_issuetype_json['statuses']:
                if raw_stat_json['id'] not in status_ids:
                    status_ids[raw_stat_json['id']] = True
                    statuses += [Status(self._options, self._session, raw_stat_json)]
        return statuses

    # non-resource
    @translate_resource_args
    def project_avatars(self, project):
        """
        Get a dict of all avatars for a project visible to the current authenticated user.

        :param project: ID or key of the project to get avatars for
        """
        return self._get_json('project/' + project + '/avatars')

    @translate_resource_args
    def create_temp_project_avatar(self, project, filename, size, avatar_img, contentType=None, auto_confirm=False):
        """
        Register an image file as a project avatar. The avatar created is temporary and must be confirmed before it can
        be used.

        Avatar images are specified by a filename, size, and file object. By default, the client will attempt to
        autodetect the picture's content type: this mechanism relies on libmagic and will not work out of the box
        on Windows systems (see http://filemagic.readthedocs.org/en/latest/guide.html for details on how to install
        support). The ``contentType`` argument can be used to explicitly set the value (note that JIRA will reject any
        type other than the well-known ones for images, e.g. ``image/jpg``, ``image/png``, etc.)

        This method returns a dict of properties that can be used to crop a subarea of a larger image for use. This
        dict should be saved and passed to :py:meth:`confirm_project_avatar` to finish the avatar creation process. If\
        you want to cut out the middleman and confirm the avatar with JIRA's default cropping, pass the 'auto_confirm'\
        argument with a truthy value and :py:meth:`confirm_project_avatar` will be called for you before this method\
        returns.

        :param project: ID or key of the project to create the avatar in
        :param filename: name of the avatar file
        :param size: size of the avatar file
        :param avatar_img: file-like object holding the avatar
        :param contentType: explicit specification for the avatar image's content-type
        :param boolean auto_confirm: whether to automatically confirm the temporary avatar by calling\
        :py:meth:`confirm_project_avatar` with the return value of this method.
        """
        size_from_file = os.path.getsize(filename)
        if size != size_from_file:
            size = size_from_file

        params = {
            'filename': filename,
            'size': size
        }

        headers = {'X-Atlassian-Token': 'no-check'}
        if contentType is not None:
            headers['content-type'] = contentType
        else:
            # try to detect content-type, this may return None
            headers['content-type'] = self._get_mime_type(avatar_img)

        url = self._get_url('project/' + project + '/avatar/temporary')
        r = self._session.post(
            url, params=params, headers=headers, data=avatar_img)

        cropping_properties = json_loads(r)
        if auto_confirm:
            return self.confirm_project_avatar(project, cropping_properties)
        else:
            return cropping_properties

    @translate_resource_args
    def confirm_project_avatar(self, project, cropping_properties):
        """
        Confirm the temporary avatar image previously uploaded with the specified cropping.

        After a successful registry with :py:meth:`create_temp_project_avatar`, use this method to confirm the avatar
        for use. The final avatar can be a subarea of the uploaded image, which is customized with the
        ``cropping_properties``: the return value of :py:meth:`create_temp_project_avatar` should be used for this
        argument.

        :param project: ID or key of the project to confirm the avatar in
        :param cropping_properties: a dict of cropping properties from :py:meth:`create_temp_project_avatar`
        """
        data = cropping_properties
        url = self._get_url('project/' + project + '/avatar')
        r = self._session.post(
            url, data=json.dumps(data))

        return json_loads(r)

    @translate_resource_args
    def set_project_avatar(self, project, avatar):
        """
        Set a project's avatar.

        :param project: ID or key of the project to set the avatar on
        :param avatar: ID of the avatar to set
        """
        self._set_avatar(
            None, self._get_url('project/' + project + '/avatar'), avatar)

    @translate_resource_args
    def delete_project_avatar(self, project, avatar):
        """
        Delete a project's avatar.

        :param project: ID or key of the project to delete the avatar from
        :param avatar: ID of the avater to delete
        """
        url = self._get_url('project/' + project + '/avatar/' + avatar)
        r = self._session.delete(url)

    @translate_resource_args
    def project_components(self, project):
        """
        Get a list of component Resources present on a project.

        :param project: ID or key of the project to get components from
        """
        r_json = self._get_json('project/' + project + '/components')
        components = [Component(
            self._options, self._session, raw_comp_json) for raw_comp_json in r_json]
        return components

    @translate_resource_args
    def project_versions(self, project):
        """
        Get a list of version Resources present on a project.

        :param project: ID or key of the project to get versions from
        """
        r_json = self._get_json('project/' + project + '/versions')
        versions = [
            Version(self._options, self._session, raw_ver_json) for raw_ver_json in r_json]
        return versions

    # non-resource
    @translate_resource_args
    def project_roles(self, project):
        """
        Get a dict of role names to resource locations for a project.

        :param project: ID or key of the project to get roles from
        """
        roles_dict = self._get_json('project/' + project + '/role')

        # TODO: return on a list of Roles()

    @translate_resource_args
    def project_role(self, project, id):
        """
        Get a role Resource.

        :param project: ID or key of the project to get the role from
        :param id: ID of the role to get
        """
        if isinstance(id, Number):
            id = "%s" % id
        return self._find_for_resource(Role, (project, id))

    # Resolutions

    def resolutions(self):
        """Get a list of resolution Resources from the server."""
        r_json = self._get_json('resolution')
        resolutions = [Resolution(
            self._options, self._session, raw_res_json) for raw_res_json in r_json]
        return resolutions

    def resolution(self, id):
        """
        Get a resolution Resource from the server.

        :param id: ID of the resolution to get
        """
        return self._find_for_resource(Resolution, id)

    # Search

    def search_issues(self, jql_str, startAt=0, maxResults=50, validate_query=True, fields=None, expand=None,
                      json_result=None):
        """
        Get a ResultList of issue Resources matching a JQL search string.

        :param jql_str: the JQL search string to use
        :param startAt: index of the first issue to return
        :param maxResults: maximum number of issues to return. Total number of results
            is available in the ``total`` attribute of the returned ResultList.
            If maxResults evaluates as False, it will try to get all issues in batches.
        :param fields: comma-separated string of issue fields to include in the results
        :param expand: extra information to fetch inside each resource
        :param json_result: JSON response will be returned when this parameter is set to True.
                Otherwise, ResultList will be returned.
        """
        # TODO what to do about the expand, which isn't related to the issues?
        if fields is None:
            fields = []

        if isinstance(fields, string_types):
            fields = fields.split(",")

        # this will translate JQL field names to REST API Name
        # most people do know the JQL names so this will help them use the API easier
        untranslate = {}  # use to add friendly aliases when we get the results back
        if self._fields:
            for i, field in enumerate(fields):
                if field in self._fields:
                    untranslate[self._fields[field]] = fields[i]
                    fields[i] = self._fields[field]

        search_params = {
            "jql": jql_str,
            "startAt": startAt,
            "maxResults": maxResults,
            "validateQuery": validate_query,
            "fields": fields,
            "expand": expand
        }
        if json_result:
            if not maxResults:
                warnings.warn('All issues cannot be fetched at once, when json_result parameter is set', Warning)
            return self._get_json('search', params=search_params)

        issues = self._fetch_pages(Issue, 'issues', 'search', startAt, maxResults, search_params)

        if untranslate:
            for i in issues:
                for k, v in iteritems(untranslate):
                    if k in i.raw['fields']:
                        i.raw['fields'][v] = i.raw['fields'][k]

        return issues

    # Security levels
    def security_level(self, id):
        """
        Get a security level Resource.

        :param id: ID of the security level to get
        """
        return self._find_for_resource(SecurityLevel, id)

    # Server info

    # non-resource
    def server_info(self):
        """Get a dict of server information for this JIRA instance."""
        retry = 0
        j = self._get_json('serverInfo')
        while not j and retry < 3:
            logging.warning("Bug https://jira.atlassian.com/browse/JRA-59676 trying again...")
            retry += 1
            j = self._get_json('serverInfo')
        return j

    def myself(self):
        """Get a dict of server information for this JIRA instance."""
        return self._get_json('myself')

    # Status

    def statuses(self):
        """Get a list of status Resources from the server."""
        r_json = self._get_json('status')
        statuses = [Status(self._options, self._session, raw_stat_json)
                    for raw_stat_json in r_json]
        return statuses

    def status(self, id):
        """
        Get a status Resource from the server.

        :param id: ID of the status resource to get
        """
        return self._find_for_resource(Status, id)

    # Users

    def user(self, id, expand=None):
        """
        Get a user Resource from the server.

        :param id: ID of the user to get
        :param expand: extra information to fetch inside each resource
        """
        user = User(self._options, self._session)
        params = {}
        if expand is not None:
            params['expand'] = expand
        user.find(urllib.pathname2url(id), params=params)
        return user


    def user_from_key(self, userkey, expand=None):
        """
        Get a User Resource from the server based on user key.
        Since JIRA 6 user key is unique, while username can change
        https://developer.atlassian.com/jiradev/latest-updates/developer-changes-for-older-jira-versions/preparing-for-jira-6-0/renamable-users-in-jira-6-0

        :param key: key of the user to get
        :param expand: extra information to fetch inside each resource
        """
        user = UserFromKey(self._options, self._session)
        params = {}
        if expand is not None:
            params['expand'] = expand
        user.find(urllib.pathname2url(userkey), params=params)
        return user


    def search_assignable_users_for_projects(self, username, projectKeys, startAt=0, maxResults=50):
        """
        Get a list of user Resources that match the search string and can be assigned issues for projects.

        :param username: a string to match usernames against
        :param projectKeys: comma-separated list of project keys to check for issue assignment permissions
        :param startAt: index of the first user to return
        :param maxResults: maximum number of users to return.
                If maxResults evaluates as False, it will try to get all users in batches.
        """
        params = {
            'username': username,
            'projectKeys': projectKeys,
        }
        return self._fetch_pages(User, None, 'user/assignable/multiProjectSearch', startAt, maxResults, params)

    def search_assignable_users_for_issues(self, username, project=None, issueKey=None, expand=None, startAt=0,
                                           maxResults=50):
        """
        Get a list of user Resources that match the search string for assigning or creating issues.

        This method is intended to find users that are eligible to create issues in a project or be assigned
        to an existing issue. When searching for eligible creators, specify a project. When searching for eligible
        assignees, specify an issue key.

        :param username: a string to match usernames against
        :param project: filter returned users by permission in this project (expected if a result will be used to \
        create an issue)
        :param issueKey: filter returned users by this issue (expected if a result will be used to edit this issue)
        :param expand: extra information to fetch inside each resource
        :param startAt: index of the first user to return
        :param maxResults: maximum number of users to return.
                If maxResults evaluates as False, it will try to get all items in batches.
        """
        params = {
            'username': username
        }
        if project is not None:
            params['project'] = project
        if issueKey is not None:
            params['issueKey'] = issueKey
        if expand is not None:
            params['expand'] = expand
        return self._fetch_pages(User, None, 'user/assignable/search', startAt, maxResults, params)

    # non-resource
    def user_avatars(self, username):
        """
        Get a dict of avatars for the specified user.

        :param username: the username to get avatars for
        """
        return self._get_json('user/avatars', params={'username': username})

    def create_temp_user_avatar(self, user, filename, size, avatar_img, contentType=None, auto_confirm=False):
        """
        Register an image file as a user avatar. The avatar created is temporary and must be confirmed before it can
        be used.

        Avatar images are specified by a filename, size, and file object. By default, the client will attempt to
        autodetect the picture's content type: this mechanism relies on ``libmagic`` and will not work out of the box
        on Windows systems (see http://filemagic.readthedocs.org/en/latest/guide.html for details on how to install
        support). The ``contentType`` argument can be used to explicitly set the value (note that JIRA will reject any
        type other than the well-known ones for images, e.g. ``image/jpg``, ``image/png``, etc.)

        This method returns a dict of properties that can be used to crop a subarea of a larger image for use. This
        dict should be saved and passed to :py:meth:`confirm_user_avatar` to finish the avatar creation process. If you
        want to cut out the middleman and confirm the avatar with JIRA's default cropping, pass the ``auto_confirm``
        argument with a truthy value and :py:meth:`confirm_user_avatar` will be called for you before this method
        returns.

        :param user: user to register the avatar for
        :param filename: name of the avatar file
        :param size: size of the avatar file
        :param avatar_img: file-like object containing the avatar
        :param contentType: explicit specification for the avatar image's content-type
        :param auto_confirm: whether to automatically confirm the temporary avatar by calling\
        :py:meth:`confirm_user_avatar` with the return value of this method.
        """
        size_from_file = os.path.getsize(filename)
        if size != size_from_file:
            size = size_from_file

        # remove path from filename
        filename = os.path.split(filename)[1]

        params = {
            'username': user,
            'filename': filename,
            'size': size
        }

        headers = {'X-Atlassian-Token': 'no-check'}
        if contentType is not None:
            headers['content-type'] = contentType
        else:
            # try to detect content-type, this may return None
            headers['content-type'] = self._get_mime_type(avatar_img)

        url = self._get_url('user/avatar/temporary')
        r = self._session.post(
            url, params=params, headers=headers, data=avatar_img)

        cropping_properties = json_loads(r)
        if auto_confirm:
            return self.confirm_user_avatar(user, cropping_properties)
        else:
            return cropping_properties

    def confirm_user_avatar(self, user, cropping_properties):
        """
        Confirm the temporary avatar image previously uploaded with the specified cropping.

        After a successful registry with :py:meth:`create_temp_user_avatar`, use this method to confirm the avatar for
        use. The final avatar can be a subarea of the uploaded image, which is customized with the
        ``cropping_properties``: the return value of :py:meth:`create_temp_user_avatar` should be used for this
        argument.

        :param user: the user to confirm the avatar for
        :param cropping_properties: a dict of cropping properties from :py:meth:`create_temp_user_avatar`
        """
        data = cropping_properties
        url = self._get_url('user/avatar')
        r = self._session.post(url, params={'username': user},
                               data=json.dumps(data))

        return json_loads(r)

    def set_user_avatar(self, username, avatar):
        """
        Set a user's avatar.

        :param username: the user to set the avatar for
        :param avatar: ID of the avatar to set
        """
        self._set_avatar(
            {'username': username}, self._get_url('user/avatar'), avatar)

    def delete_user_avatar(self, username, avatar):
        """
        Delete a user's avatar.

        :param username: the user to delete the avatar from
        :param avatar: ID of the avatar to remove
        """
        params = {'username': username}
        url = self._get_url('user/avatar/' + avatar)
        r = self._session.delete(url, params=params)

    def search_users(self, user, startAt=0, maxResults=50, includeActive=True, includeInactive=False):
        """
        Get a list of user Resources that match the specified search string.

        :param user: a string to match usernames, name or email against.
        :param startAt: index of the first user to return.
        :param maxResults: maximum number of users to return.
                If maxResults evaluates as False, it will try to get all items in batches.
        :param includeActive: If true, then active users are included in the results.
        :param includeInactive: If true, then inactive users are included in the results.
        """
        params = {
            'username': user,
            'includeActive': includeActive,
            'includeInactive': includeInactive
        }
        return self._fetch_pages(User, None, 'user/search', startAt, maxResults, params)

    def search_allowed_users_for_issue(self, user, issueKey=None, projectKey=None, startAt=0, maxResults=50):
        """
        Get a list of user Resources that match a username string and have browse permission for the issue or
        project.

        :param user: a string to match usernames against.
        :param issueKey: find users with browse permission for this issue.
        :param projectKey: find users with browse permission for this project.
        :param startAt: index of the first user to return.
        :param maxResults: maximum number of users to return.
                If maxResults evaluates as False, it will try to get all items in batches.
        """
        params = {
            'username': user
        }
        if issueKey is not None:
            params['issueKey'] = issueKey
        if projectKey is not None:
            params['projectKey'] = projectKey
        return self._fetch_pages(User, None, 'user/viewissue/search', startAt, maxResults, params)

    # Versions

    @translate_resource_args
    def create_version(self, name, project, description=None, releaseDate=None, startDate=None, archived=False,
                       released=False):
        """
        Create a version in a project and return a Resource for it.

        :param name: name of the version to create
        :param project: key of the project to create the version in
        :param description: a description of the version
        :param releaseDate: the release date assigned to the version
        :param startDate: The start date for the version
        """
        data = {
            'name': name,
            'project': project,
            'archived': archived,
            'released': released
        }
        if description is not None:
            data['description'] = description
        if releaseDate is not None:
            data['releaseDate'] = releaseDate
        if startDate is not None:
            data['startDate'] = startDate

        url = self._get_url('version')
        r = self._session.post(
            url, data=json.dumps(data))

        version = Version(self._options, self._session, raw=json_loads(r))
        return version

    def move_version(self, id, after=None, position=None):
        """
        Move a version within a project's ordered version list and return a new version Resource for it. One,
        but not both, of ``after`` and ``position`` must be specified.

        :param id: ID of the version to move
        :param after: the self attribute of a version to place the specified version after (that is, higher in the list)
        :param position: the absolute position to move this version to: must be one of ``First``, ``Last``,\
        ``Earlier``, or ``Later``
        """
        data = {}
        if after is not None:
            data['after'] = after
        elif position is not None:
            data['position'] = position

        url = self._get_url('version/' + id + '/move')
        r = self._session.post(
            url, data=json.dumps(data))

        version = Version(self._options, self._session, raw=json_loads(r))
        return version

    def version(self, id, expand=None):
        """
        Get a version Resource.

        :param id: ID of the version to get
        :param expand: extra information to fetch inside each resource
        """
        version = Version(self._options, self._session)
        params = {}
        if expand is not None:
            params['expand'] = expand
        version.find(id, params=params)
        return version

    def version_count_related_issues(self, id):
        """
        Get a dict of the counts of issues fixed and affected by a version.

        :param id: the version to count issues for
        """
        r_json = self._get_json('version/' + id + '/relatedIssueCounts')
        del r_json['self']  # this isn't really an addressable resource
        return r_json

    def version_count_unresolved_issues(self, id):
        """
        Get the number of unresolved issues for a version.

        :param id: ID of the version to count issues for
        """
        return self._get_json('version/' + id + '/unresolvedIssueCount')['issuesUnresolvedCount']

    # Session authentication

    def session(self):
        """Get a dict of the current authenticated user's session information."""
        url = '{server}/rest/auth/1/session'.format(**self._options)

        if type(self._session.auth) is tuple:
            authentication_data = {
                'username': self._session.auth[0], 'password': self._session.auth[1]}
            r = self._session.post(url, data=json.dumps(authentication_data))
        else:
            r = self._session.get(url)

        user = User(self._options, self._session, json_loads(r))
        return user

    def kill_session(self):
        """Destroy the session of the current authenticated user."""
        url = self._options['server'] + '/rest/auth/latest/session'
        r = self._session.delete(url)

    # Websudo

    def kill_websudo(self):
        """Destroy the user's current WebSudo session."""
        url = self._options['server'] + '/rest/auth/1/websudo'
        r = self._session.delete(url)

    # Utilities
    def _create_http_basic_session(self, username, password):
        verify = self._options['verify']
        self._session = ResilientSession()
        self._session.verify = verify
        self._session.auth = (username, password)
        self._session.cert = self._options['client_cert']

    def _create_oauth_session(self, oauth):
        verify = self._options['verify']

        from requests_oauthlib import OAuth1
        from oauthlib.oauth1 import SIGNATURE_RSA

        oauth = OAuth1(
            oauth['consumer_key'],
            rsa_key=oauth['key_cert'],
            signature_method=SIGNATURE_RSA,
            resource_owner_key=oauth['access_token'],
            resource_owner_secret=oauth['access_token_secret']
        )
        self._session = ResilientSession()
        self._session.verify = verify
        self._session.auth = oauth

    @staticmethod
    def _timestamp(dt=None):
        t = datetime.datetime.utcnow()
        if dt is not None:
            t += dt
        return calendar.timegm(t.timetuple())

    def _create_jwt_session(self, jwt):
        try:
            jwt_auth = JWTAuth(jwt['secret'], alg='HS256')
        except NameError as e:
            logging.error("JWT authentication requires requests_jwt")
            raise e
        jwt_auth.add_field("iat", lambda req: JIRA._timestamp())
        jwt_auth.add_field("exp", lambda req: JIRA._timestamp(datetime.timedelta(minutes=3)))
        jwt_auth.add_field("qsh", QshGenerator(self._options['context_path']))
        for f in jwt['payload'].items():
            jwt_auth.add_field(f[0], f[1])
        self._session = ResilientSession()
        self._session.verify = self._options['verify']
        self._session.auth = jwt_auth

    def _set_avatar(self, params, url, avatar):
        data = {
            'id': avatar
        }
        r = self._session.put(url, params=params, data=json.dumps(data))

    def _get_url(self, path, base=JIRA_BASE_URL):
        options = self._options.copy()
        options.update({'path': path})
        return base.format(**options)

    def _get_json(self, path, params=None, base=JIRA_BASE_URL):
        url = self._get_url(path, base)
        r = self._session.get(url, params=params)
        try:
            r_json = json_loads(r)
        except ValueError as e:
            logging.error("%s\n%s" % (e, r.text))
            raise e
        return r_json

    def _find_for_resource(self, resource_cls, ids, expand=None):
        resource = resource_cls(self._options, self._session)
        params = {}
        if expand is not None:
            params['expand'] = expand
        resource.find(id=ids, params=params)
        return resource

    def _try_magic(self):
        try:
            import magic
            import weakref
        except ImportError:
            self._magic = None
        else:
            try:
                _magic = magic.Magic(flags=magic.MAGIC_MIME_TYPE)

                def cleanup(x):
                    _magic.close()
                self._magic_weakref = weakref.ref(self, cleanup)
                self._magic = _magic
            except TypeError:
                self._magic = None
            except AttributeError:
                self._magic = None

    def _get_mime_type(self, buff):
        if self._magic is not None:
            return self._magic.id_buffer(buff)
        else:
            try:
                return mimetypes.guess_type("f." + imghdr.what(0, buff))[0]
            except (IOError, TypeError):
                logging.warning("Couldn't detect content type of avatar image"
                                ". Specify the 'contentType' parameter explicitly.")
                return None

    def email_user(self, user, body, title="JIRA Notification"):
        """
        TBD:
        """
        url = self._options['server'] + \
            '/secure/admin/groovy/CannedScriptRunner.jspa'
        payload = {
            'cannedScript': 'com.onresolve.jira.groovy.canned.workflow.postfunctions.SendCustomEmail',
            'cannedScriptArgs_FIELD_CONDITION': '',
            'cannedScriptArgs_FIELD_EMAIL_TEMPLATE': body,
            'cannedScriptArgs_FIELD_EMAIL_SUBJECT_TEMPLATE': title,
            'cannedScriptArgs_FIELD_EMAIL_FORMAT': 'TEXT',
            'cannedScriptArgs_FIELD_TO_ADDRESSES': self.user(user).emailAddress,
            'cannedScriptArgs_FIELD_TO_USER_FIELDS': '',
            'cannedScriptArgs_FIELD_INCLUDE_ATTACHMENTS': 'FIELD_INCLUDE_ATTACHMENTS_NONE',
            'cannedScriptArgs_FIELD_FROM': '',
            'cannedScriptArgs_FIELD_PREVIEW_ISSUE': '',
            'cannedScript': 'com.onresolve.jira.groovy.canned.workflow.postfunctions.SendCustomEmail',
            'id': '',
            'Preview': 'Preview',
        }

        r = self._session.post(
            url, headers=self._options['headers'], data=payload)
        open("/tmp/jira_email_user_%s.html" % user, "w").write(r.text)

    def rename_user(self, old_user, new_user):
        """
        Rename a JIRA user. Current implementation relies on third party plugin but in the future it may use embedded JIRA functionality.

        :param old_user: string with username login
        :param new_user: string with username login
        """

        if self._version >= (6, 0, 0):

            url = self._options['server'] + '/rest/api/latest/user'
            payload = {
                "name": new_user,
            }
            params = {
                'username': old_user
            }

            # raw displayName
            logging.debug("renaming %s" % self.user(old_user).emailAddress)

            r = self._session.put(url, params=params,
                                  data=json.dumps(payload))

        else:
            # old implementation needed the ScripRunner plugin
            merge = "true"
            try:
                self.user(new_user)
            except:
                merge = "false"

            url = self._options['server'] + '/secure/admin/groovy/CannedScriptRunner.jspa#result'
            payload = {
                "cannedScript": "com.onresolve.jira.groovy.canned.admin.RenameUser",
                "cannedScriptArgs_FIELD_FROM_USER_ID": old_user,
                "cannedScriptArgs_FIELD_TO_USER_ID": new_user,
                "cannedScriptArgs_FIELD_MERGE": merge,
                "id": "",
                "RunCanned": "Run",
            }

            # raw displayName
            logging.debug("renaming %s" % self.user(old_user).emailAddress)

            r = self._session.post(
                url, headers=self._options['headers'], data=payload)
            if r.status_code == 404:
                logging.error(
                    "In order to be able to use rename_user() you need to install Script Runner plugin. See https://marketplace.atlassian.com/plugins/com.onresolve.jira.groovy.groovyrunner")
                return False
            if r.status_code != 200:
                logging.error(r.status_code)

            if re.compile("XSRF Security Token Missing").search(r.content):
                logging.fatal(
                    "Reconfigure JIRA and disable XSRF in order to be able call this. See https://developer.atlassian.com/display/JIRADEV/Form+Token+Handling")
                return False

            open("/tmp/jira_rename_user_%s_to%s.html" %
                 (old_user, new_user), "w").write(r.content)

            msg = r.status_code
            m = re.search("<span class=\"errMsg\">(.*)<\/span>", r.content)
            if m:
                msg = m.group(1)
                logging.error(msg)
                return False
                # <span class="errMsg">Target user ID must exist already for a merge</span>
            p = re.compile("type=\"hidden\" name=\"cannedScriptArgs_Hidden_output\" value=\"(.*?)\"\/>",
                           re.MULTILINE | re.DOTALL)
            m = p.search(r.content)
            if m:
                h = html_parser.HTMLParser()
                msg = h.unescape(m.group(1))
                logging.info(msg)

            # let's check if the user still exists
            try:
                self.user(old_user)
            except Exception as e:
                logging.error("User %s does not exists. %s", old_user, e)
                return msg

            logging.error(msg + '\n' + "User %s does still exists after rename, that's clearly a problem." % old_user)
            return False

    def delete_user(self, username):

        url = self._options['server'] + '/rest/api/latest/user/?username=%s' % username

        r = self._session.delete(url)
        if 200 <= r.status_code <= 299:
            return True
        else:
            logging.error(r.status_code)
            return False

    def reindex(self, force=False, background=True):
        """
        Start jira re-indexing. Returns True if reindexing is in progress or not needed, or False.

        If you call reindex() without any parameters it will perform a backfround reindex only if JIRA thinks it should do it.

        :param force: reindex even if JIRA doesn'tt say this is needed, False by default.
        :param background: reindex inde background, slower but does not impact the users, defaults to True.
        """
        # /secure/admin/IndexAdmin.jspa
        # /secure/admin/jira/IndexProgress.jspa?taskId=1
        if background:
            indexingStrategy = 'background'
        else:
            indexingStrategy = 'stoptheworld'

        url = self._options['server'] + '/secure/admin/jira/IndexReIndex.jspa'

        r = self._session.get(url, headers=self._options['headers'])
        if r.status_code == 503:
            # logging.warning("JIRA returned 503, this could mean that a full reindex is in progress.")
            return 503

        if not r.text.find("To perform the re-index now, please go to the") and force is False:
            return True

        if r.text.find('All issues are being re-indexed'):
            logging.warning("JIRA re-indexing is already running.")
            return True  # still reindexing is considered still a success

        if r.text.find('To perform the re-index now, please go to the') or force:
            r = self._session.post(url, headers=self._options['headers'],
                                   params={"indexingStrategy": indexingStrategy, "reindex": "Re-Index"})
            if r.text.find('All issues are being re-indexed') != -1:
                return True
            else:
                logging.error("Failed to reindex jira, probably a bug.")
                return False

    def backup(self, filename='backup.zip'):
        """
        Will call jira export to backup as zipped xml. Returning with success does not mean that the backup process finished.
        """
        url = self._options['server'] + '/secure/admin/XmlBackup.jspa'
        payload = {'filename': filename}
        r = self._session.post(
            url, headers=self._options['headers'], data=payload)
        if r.status_code == 200:
            return True
        else:
            logging.warning(
                'Got %s response from calling backup.' % r.status_code)
            return r.status_code

    def current_user(self):
        if not hasattr(self, '_serverInfo') or 'username' not in self._serverInfo:

            url = self._get_url('serverInfo')
            r = self._session.get(url, headers=self._options['headers'])

            r_json = json_loads(r)
            if 'x-ausername' in r.headers:
                r_json['username'] = r.headers['x-ausername']
            else:
                r_json['username'] = None
            self._serverInfo = r_json
            # del r_json['self']  # this isn't really an addressable resource
        return self._serverInfo['username']

    def delete_project(self, pid):
        """
        Project can be id, project key or project name. It will return False if it fails.
        """
        found = False
        try:
            if not str(int(pid)) == pid:
                found = True
        except Exception as e:
            r_json = self._get_json('project')
            for e in r_json:
                if e['key'] == pid or e['name'] == pid:
                    pid = e['id']
                    found = True
                    break
            if not found:
                logging.error("Unable to recognize project `%s`" % pid)
                return False

        uri = '/secure/project/DeleteProject.jspa'
        url = self._options['server'] + uri
        payload = {'pid': pid, 'Delete': 'Delete', 'confirm': 'true'}
        # try:
        #     r = self._gain_sudo_session(payload, uri)
        #     if r.status_code != 200 or not self._check_for_html_error(r.text):
        #         return False
        # except JIRAError as e:
        #     raise JIRAError(0, "You must have global administrator rights to delete projects.")
        #     return False

        r = self._session.post(
            url, headers=CaseInsensitiveDict({'content-type': 'application/x-www-form-urlencoded'}), data=payload)

        if r.status_code == 200:
            return self._check_for_html_error(r.text)
        else:
            logging.warning(
                'Got %s response from calling delete_project.' % r.status_code)
            return r.status_code

    def _gain_sudo_session(self, options, destination):
        url = self._options['server'] + '/secure/admin/WebSudoAuthenticate.jspa'

        if not self._session.auth:
            self._session.auth = get_netrc_auth(url)

        payload = {
            'webSudoPassword': self._session.auth[1],
            'webSudoDestination': destination,
            'webSudoIsPost': 'true',
        }

        payload.update(options)

        return self._session.post(
            url, headers=CaseInsensitiveDict({'content-type': 'application/x-www-form-urlencoded'}), data=payload)

    def create_project(self, key, name=None, assignee=None, type="Software"):
        """
        Key is mandatory and has to match JIRA project key requirements, usually only 2-10 uppercase characters.
        If name is not specified it will use the key value.
        If assignee is not specified it will use current user.
        The returned value should evaluate to False if it fails otherwise it will be the new project id.
        """
        if assignee is None:
            assignee = self.current_user()
        if name is None:
            name = key
        if key.upper() != key or not key.isalpha() or len(key) < 2 or len(key) > 10:
            logging.error(
                'key parameter is not all uppercase alphanumeric of length between 2 and 10')
            return False
        url = self._options['server'] + \
            '/rest/project-templates/latest/templates'

        r = self._session.get(url)
        j = json_loads(r)

        template_key = None
        templates = []
        for template in _get_template_list(j):
            templates.append(template['name'])
            if template['name'] in ['JIRA Classic', 'JIRA Default Schemes', 'Basic software development']:
                template_key = template['projectTemplateModuleCompleteKey']
                break

        if not template_key:
            raise JIRAError(
                "Unable to find a suitable project template to use. Found only: %s" % json.dumps(j))

        payload = {'name': name,
                   'key': key,
                   'keyEdited': 'false',
                   #'projectTemplate': 'com.atlassian.jira-core-project-templates:jira-issuetracking',
                   #'permissionScheme': '',
                   'projectTemplateWebItemKey': template_key,
                   'projectTemplateModuleKey': template_key,
                   'lead': assignee,
                   #'assigneeType': '2',
                   }

        if self._version[0] > 6:
            # JIRA versions before 7 will throw an error if we specify type parameter
            payload['type'] = type

        headers = CaseInsensitiveDict(
            {'Content-Type': 'application/x-www-form-urlencoded'})

        r = self._session.post(url, data=payload, headers=headers)

        if r.status_code == 200:
            r_json = json_loads(r)
            return r_json

        f = tempfile.NamedTemporaryFile(
            suffix='.html', prefix='python-jira-error-create-project-', delete=False)
        f.write(r.text)

        if self.logging:
            logging.error(
                "Unexpected result while running create project. Server response saved in %s for further investigation [HTTP response=%s]." % (
                    f.name, r.status_code))
        return False

    def add_user(self, username, email, directoryId=1, password=None,
                 fullname=None, notify=False, active=True, ignore_existing=False):
        '''
        Creates a new JIRA user
        :param username: the username of the new user
        :type username: ``str``
        :param email: email address of the new user
        :type email: ``str``
        :param directoryId: the directory ID the new user should be a part of
        :type directoryId: ``int``
        :param password: Optional, the password for the new user
        :type password: ``str``
        :param fullname: Optional, the full name of the new user
        :type fullname: ``str``
        :param notify: Whether or not to send a notification to the new user
        :type notify: ``bool``
        :param active: Whether or not to make the new user active upon creation
        :type active: ``bool``
        '''
        if not fullname:
            fullname = username
        # TODO: default the directoryID to the first directory in jira instead
        # of 1 which is the internal one.
        url = self._options['server'] + '/rest/api/latest/user'

        # implementation based on
        # https://docs.atlassian.com/jira/REST/ondemand/#d2e5173
        x = OrderedDict()

        x['displayName'] = fullname
        x['emailAddress'] = email
        x['name'] = username
        if password:
            x['password'] = password
        if notify:
            x['notification'] = 'True'

        payload = json.dumps(x)
        try:
            self._session.post(url, data=payload)
        except JIRAError as e:
            err = e.response.json()['errors']
            if 'username' in err and err['username'] == 'A user with that username already exists.' and ignore_existing:
                return True
            raise e
        return True

    def add_user_to_group(self, username, group):
        '''
        Adds a user to an existing group.
        :param username: Username that will be added to specified group.
        :param group: Group that the user will be added to.
        :return: Boolean, True for success, false for failure.
        '''
        url = self._options['server'] + '/rest/api/latest/group/user'
        x = {'groupname': group}
        y = {'name': username}

        payload = json.dumps(y)

        self._session.post(url, params=x, data=payload)

        return True

    def remove_user_from_group(self, username, groupname):
        '''
        Removes a user from a group.
        :param username: The user to remove from the group.
        :param groupname: The group that the user will be removed from.
        :return:
        '''
        url = self._options['server'] + '/rest/api/latest/group/user'
        x = {'groupname': groupname,
             'username': username}

        self._session.delete(url, params=x)

        return True

    # Experimental
    # Experimental support for iDalko Grid, expect API to change as it's using private APIs currently
    # https://support.idalko.com/browse/IGRID-1017
    def get_igrid(self, issueid, customfield, schemeid):
        url = self._options['server'] + '/rest/idalko-igrid/1.0/datagrid/data'
        if str(customfield).isdigit():
            customfield = "customfield_%s" % customfield
        params = {
            #'_mode':'view',
            '_issueId': issueid,
            '_fieldId': customfield,
            '_confSchemeId': schemeid,
            #'validate':True,
            #'_search':False,
            #'rows':100,
            #'page':1,
            #'sidx':'DEFAULT',
            #'sord':'asc',
        }
        r = self._session.get(
            url, headers=self._options['headers'], params=params)
        return json_loads(r)

    # Jira Agile specific methods (GreenHopper)
    """
    Define the functions that interact with GreenHopper.
    """

    @translate_resource_args
    def boards(self, startAt=0, maxResults=50, type=None, name=None):
        """
        Get a list of board resources.

        :param startAt: The starting index of the returned boards. Base index: 0.
        :param maxResults: The maximum number of boards to return per page. Default: 50
        :param type: Filters results to boards of the specified type. Valid values: scrum, kanban.
        :param name: Filters results to boards that match or partially match the specified name.
        :rtype: ResultList[Board]

        When old GreenHopper private API is used, paging is not enabled and all parameters are ignored.
        """

        params = {}
        if type:
            params['type'] = type
        if name:
            params['name'] = name

        if self._options['agile_rest_path'] == GreenHopperResource.GREENHOPPER_REST_PATH:
            # Old, private API did not support pagination, all records were present in response,
            #   and no parameters were supported.
            if startAt or maxResults or params:
                warnings.warn('Old private GreenHopper API is used, all parameters will be ignored.', Warning)

            r_json = self._get_json('rapidviews/list', base=self.AGILE_BASE_URL)
            boards = [Board(self._options, self._session, raw_boards_json) for raw_boards_json in r_json['views']]
            return ResultList(boards, 0, len(boards), len(boards), True)
        else:
            return self._fetch_pages(Board, 'values', 'board', startAt, maxResults, params, base=self.AGILE_BASE_URL)

    @translate_resource_args
    def sprints(self, board_id, extended=False, startAt=0, maxResults=50, state=None):
        """
        Get a list of sprint GreenHopperResources.

        :param board_id: the board to get sprints from
        :param extended: Used only by old GreenHopper API to fetch additional information like
            startDate, endDate, completeDate, much slower because it requires an additional requests for each sprint.
            New JIRA Agile API always returns this information without a need for additional requests.
        :param startAt: the index of the first sprint to return (0 based)
        :param maxResults: the maximum number of sprints to return
        :param state: Filters results to sprints in specified states. Valid values: future, active, closed.
            You can define multiple states separated by commas

        :rtype dict
        :return (content depends on API version, but always contains id, name, state, startDate and endDate)

        When old GreenHopper private API is used, paging is not enabled,
            and `startAt`, `maxResults` and `state` parameters are ignored.
        """

        params = {}
        if state:
            if isinstance(state, string_types):
                state = state.split(",")
            params['state'] = state

        if self._options['agile_rest_path'] == GreenHopperResource.GREENHOPPER_REST_PATH:
            r_json = self._get_json('sprintquery/%s?includeHistoricSprints=true&includeFutureSprints=true' % board_id,
                                    base=self.AGILE_BASE_URL)

            if params:
                warnings.warn('Old private GreenHopper API is used, parameters %s will be ignored.' % params, Warning)

            if extended:
                sprints = [Sprint(self._options, self._session, self.sprint_info(None, raw_sprints_json['id']))
                           for raw_sprints_json in r_json['sprints']]
            else:
                sprints = [Sprint(self._options, self._session, raw_sprints_json)
                           for raw_sprints_json in r_json['sprints']]

            return ResultList(sprints, 0, len(sprints), len(sprints), True)
        else:
            return self._fetch_pages(Sprint, 'values', 'board/%s/sprint' % board_id, startAt, maxResults, params,
                                     self.AGILE_BASE_URL)

    def sprints_by_name(self, id, extended=False):
        sprints = {}
        for s in self.sprints(id, extended=extended):
            if s.name not in sprints:
                sprints[s.name] = s.raw
            else:
                raise (Exception(
                    "Fatal error, duplicate Sprint Name (%s) found on board %s." % (s.name, id)))
        return sprints

    def update_sprint(self, id, name=None, startDate=None, endDate=None, state=None):
        payload = {}
        if name:
            payload['name'] = name
        if startDate:
            payload['startDate'] = startDate
        if endDate:
            payload['endDate'] = endDate
        if state:
            if self._options['agile_rest_path'] != GreenHopperResource.GREENHOPPER_REST_PATH:
                raise NotImplementedError('Public JIRA API does not support state update')
            payload['state'] = state

        url = self._get_url('sprint/%s' % id, base=self.AGILE_BASE_URL)
        r = self._session.put(
            url, data=json.dumps(payload))

        return json_loads(r)

    def completed_issues(self, board_id, sprint_id):
        """
        Return the completed issues for ``board_id`` and ``sprint_id``.

        :param board_id: the board retrieving issues from
        :param sprint_id: the sprint retieving issues from
        """
        # TODO need a better way to provide all the info from the sprintreport
        # incompletedIssues went to backlog but not it not completed
        # issueKeysAddedDuringSprint used to mark some with a * ?
        # puntedIssues are for scope change?

        if self._options['agile_rest_path'] != GreenHopperResource.GREENHOPPER_REST_PATH:
            raise NotImplementedError('JIRA Agile Public API does not support this request')
        warnings.warn('JIRA.completed_issues uses deprecated API, that is going to be removed on the 1st February 2016',
                      DeprecationWarning)
        r_json = self._get_json('rapid/charts/sprintreport?rapidViewId=%s&sprintId=%s' % (board_id, sprint_id),
                                base=self.AGILE_BASE_URL)
        issues = [Issue(self._options, self._session, raw_issues_json) for raw_issues_json in
                  r_json['contents']['completedIssues']]
        return issues

    def completedIssuesEstimateSum(self, board_id, sprint_id):
        """
        Return the total completed points this sprint.
        """
        if self._options['agile_rest_path'] != GreenHopperResource.GREENHOPPER_REST_PATH:
            raise NotImplementedError('JIRA Agile Public API does not support this request')
        warnings.warn('JIRA.completedIssuesEstimateSum uses deprecated API, that is going to be removed'
                      ' on the 1st February 2016',
                      DeprecationWarning)
        return self._get_json('rapid/charts/sprintreport?rapidViewId=%s&sprintId=%s' % (board_id, sprint_id),
                              base=self.AGILE_BASE_URL)['contents']['completedIssuesEstimateSum']['value']

    def incompleted_issues(self, board_id, sprint_id):
        """
        Return the completed issues for the sprint
        """
        if self._options['agile_rest_path'] != GreenHopperResource.GREENHOPPER_REST_PATH:
            raise NotImplementedError('JIRA Agile Public API does not support this request')
        warnings.warn('JIRA.incompleted_issues uses deprecated API, that is going to be removed'
                      'on the 1st February 2016',
                      DeprecationWarning)
        r_json = self._get_json('rapid/charts/sprintreport?rapidViewId=%s&sprintId=%s' % (board_id, sprint_id),
                                base=self.AGILE_BASE_URL)

        # r_json does not contain 'incompletedIssues' changed to 'issuesNotCompletedInCurrentSprint'
        issues = [Issue(self._options, self._session, raw_issues_json) for raw_issues_json in
                  r_json['contents']['issuesNotCompletedInCurrentSprint']]
        return issues

    # TODO: remove sprint_info() method, sprint() method suit the convention more
    def sprint_info(self, board_id, sprint_id):
        """
        Return the information about a sprint.

        :param board_id: the board retrieving issues from. Deprecated and ignored.
        :param sprint_id: the sprint retrieving issues from
        """
        sprint = Sprint(self._options, self._session)
        sprint.find(sprint_id)
        return sprint.raw

    def sprint(self, id):
        sprint = Sprint(self._options, self._session)
        sprint.find(id)
        return sprint

    # TODO: remove this as we do have Board.delete()
    def delete_board(self, id):
        """ Deletes an agile board. """
        board = Board(self._options, self._session, raw={'id': id})
        board.delete()

    def create_board(self, name, project_ids, preset="scrum"):
        """
        Create a new board for the ``project_ids``.

        :param name: name of the board
        :param project_ids: the projects to create the board in
        :param preset: what preset to use for this board
        :type preset: 'kanban', 'scrum', 'diy'
        """
        if self._options['agile_rest_path'] != GreenHopperResource.GREENHOPPER_REST_PATH:
            raise NotImplementedError('JIRA Agile Public API does not support this request')

        payload = {}
        if isinstance(project_ids, string_types):
            ids = []
            for p in project_ids.split(','):
                ids.append(self.project(p).id)
            project_ids = ','.join(ids)

        payload['name'] = name
        if isinstance(project_ids, string_types):
            project_ids = project_ids.split(',')
        payload['projectIds'] = project_ids
        payload['preset'] = preset
        url = self._get_url(
            'rapidview/create/presets', base=self.AGILE_BASE_URL)
        r = self._session.post(
            url, data=json.dumps(payload))

        raw_issue_json = json_loads(r)
        return Board(self._options, self._session, raw=raw_issue_json)

    def create_sprint(self, name, board_id, startDate=None, endDate=None):
        """
        Create a new sprint for the ``board_id``.

        :param name: name of the sprint
        :param board_id: the board to add the sprint to
        """

        payload = {'name': name}
        if startDate:
            payload["startDate"] = startDate
        if endDate:
            payload["endDate"] = endDate

        if self._options['agile_rest_path'] == GreenHopperResource.GREENHOPPER_REST_PATH:
            url = self._get_url('sprint/%s' % board_id, base=self.AGILE_BASE_URL)
            r = self._session.post(url)
            raw_issue_json = json_loads(r)
            """ now r contains something like:
            {
                  "id": 742,
                  "name": "Sprint 89",
                  "state": "FUTURE",
                  "linkedPagesCount": 0,
                  "startDate": "None",
                  "endDate": "None",
                  "completeDate": "None",
                  "remoteLinks": []
            }"""

            url = self._get_url(
                'sprint/%s' % raw_issue_json['id'], base=self.AGILE_BASE_URL)
            r = self._session.put(
                url, data=json.dumps(payload))
            raw_issue_json = json_loads(r)
        else:
            url = self._get_url('sprint', base=self.AGILE_BASE_URL)
            payload['originBoardId'] = board_id
            r = self._session.post(url, data=json.dumps(payload))
            raw_issue_json = json_loads(r)

        return Sprint(self._options, self._session, raw=raw_issue_json)

    def add_issues_to_sprint(self, sprint_id, issue_keys):
        """
        Add the issues in ``issue_keys`` to the ``sprint_id``. The sprint must
        be started but not completed.

        If a sprint was completed, then have to also edit the history of the
        issue so that it was added to the sprint before it was completed,
        preferably before it started. A completed sprint's issues also all have
        a resolution set before the completion date.

        If a sprint was not started, then have to edit the marker and copy the
        rank of each issue too.

        :param sprint_id: the sprint to add issues to
        :param issue_keys: the issues to add to the sprint
        """

        if self._options['agile_rest_path'] == GreenHopperResource.AGILE_BASE_REST_PATH:
            url = self._get_url('sprint/%s/issue' % sprint_id, base=self.AGILE_BASE_URL)
            payload = {'issues': issue_keys}
            try:
                self._session.post(url, data=json.dumps(payload))
            except JIRAError as e:
                if e.status_code == 404:
                    warnings.warn('Status code 404 may mean, that too old JIRA Agile version is installed.'
                                  ' At least version 6.7.10 is required.')
                raise
        elif self._options['agile_rest_path'] == GreenHopperResource.GREENHOPPER_REST_PATH:
            # In old, private API the function does not exist anymore and we need to use
            # issue.update() to perform this operation
            # Workaround based on https://answers.atlassian.com/questions/277651/jira-agile-rest-api-example

            # Get the customFieldId for "Sprint"
            sprint_field_name = "Sprint"
            sprint_field_id = [f['schema']['customId'] for f in self.fields()
                               if f['name'] == sprint_field_name][0]

            data = {'idOrKeys': issue_keys, 'customFieldId': sprint_field_id,
                    'sprintId': sprint_id, 'addToBacklog': False}
            url = self._get_url('sprint/rank', base=self.AGILE_BASE_URL)
            r = self._session.put(url, data=json.dumps(data))
        else:
            raise NotImplementedError('No API for adding issues to sprint for agile_rest_path="%s"' %
                                      self._options['agile_rest_path'])

    def add_issues_to_epic(self, epic_id, issue_keys, ignore_epics=True):
        """
        Add the issues in ``issue_keys`` to the ``epic_id``.

        :param epic_id: the epic to add issues to
        :param issue_keys: the issues to add to the epic
        :param ignore_epics: ignore any issues listed in ``issue_keys`` that are epics
        """
        if self._options['agile_rest_path'] != GreenHopperResource.GREENHOPPER_REST_PATH:
            # TODO: simulate functionality using issue.update()?
            raise NotImplementedError('JIRA Agile Public API does not support this request')

        data = {}
        data['issueKeys'] = issue_keys
        data['ignoreEpics'] = ignore_epics
        url = self._get_url('epics/%s/add' %
                            epic_id, base=self.AGILE_BASE_URL)
        r = self._session.put(
            url, data=json.dumps(data))

    # TODO: Both GreenHopper and new JIRA Agile API support moving more than one issue.
    def rank(self, issue, next_issue):
        """
        Rank an issue before another using the default Ranking field, the one named 'Rank'.

        :param issue: issue key of the issue to be ranked before the second one.
        :param next_issue: issue key of the second issue.
        """
        if not self._rank:
            for field in self.fields():
                if field['name'] == 'Rank':
                    if field['schema']['custom'] == "com.pyxis.greenhopper.jira:gh-lexo-rank":
                        self._rank = field['schema']['customId']
                        break
                    elif field['schema']['custom'] == "com.pyxis.greenhopper.jira:gh-global-rank":
                        # Obsolete since JIRA v6.3.13.1
                        self._rank = field['schema']['customId']

        if self._options['agile_rest_path'] == GreenHopperResource.AGILE_BASE_REST_PATH:
            url = self._get_url('issue/rank', base=self.AGILE_BASE_URL)
            payload = {'issues': [issue], 'rankBeforeIssue': next_issue, 'rankCustomFieldId': self._rank}
            try:
                r = self._session.put(url, data=json.dumps(payload))
            except JIRAError as e:
                if e.status_code == 404:
                    warnings.warn('Status code 404 may mean, that too old JIRA Agile version is installed.'
                                  ' At least version 6.7.10 is required.')
                raise
        elif self._options['agile_rest_path'] == GreenHopperResource.GREENHOPPER_REST_PATH:
            # {"issueKeys":["ANERDS-102"],"rankBeforeKey":"ANERDS-94","rankAfterKey":"ANERDS-7","customFieldId":11431}
            data = {
                "issueKeys": [issue], "rankBeforeKey": next_issue, "customFieldId": self._rank}
            url = self._get_url('rank', base=self.AGILE_BASE_URL)
            r = self._session.put(url, data=json.dumps(data))
        else:
            raise NotImplementedError('No API for ranking issues for agile_rest_path="%s"' %
                                      self._options['agile_rest_path'])

    # JIRA Software - agile boards specific methods
    def board_configuration(self, board_id):
        """
        Get configuration of the rapid board.

        :param board_id: ID or key of the board to get configuration for
        """
        r_json = self._get_json('board/' + str(board_id) + '/configuration', base=self.JIRA_SOFTWARE_BASE_URL)
        return r_json

    def board(self, board_id):
        """
        Get configuration of the rapid board.

        :param board_id: ID or key of the board to get configuration for
        """
        r_json = self._get_json('board/' + str(board_id), base=self.JIRA_SOFTWARE_BASE_URL)
        return r_json


class GreenHopper(JIRA):

    def __init__(self, options=None, basic_auth=None, oauth=None, async=None):
        warnings.warn(
            "GreenHopper() class is deprecated, just use JIRA() instead.", DeprecationWarning)
        JIRA.__init__(
            self, options=options, basic_auth=basic_auth, oauth=oauth, async=async)
