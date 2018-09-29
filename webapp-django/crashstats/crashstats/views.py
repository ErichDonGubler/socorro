import datetime
import json
import os

from django import http
from django.conf import settings
from django.core.urlresolvers import reverse
from django.contrib.auth.decorators import permission_required
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.utils.http import urlquote

from csp.decorators import csp_update

from socorro.external.crashstorage_base import CrashIDNotFound
from . import forms, models, utils
from .decorators import pass_default_context

from crashstats.supersearch.models import SuperSearchFields


# To prevent running in to a known Python bug
# (http://bugs.python.org/issue7980)
# we, here at "import time" (as opposed to run time) make use of time.strptime
# at least once
datetime.datetime.strptime('2013-07-15 10:00:00', '%Y-%m-%d %H:%M:%S')


def ratelimit_blocked(request, exception):
    # http://tools.ietf.org/html/rfc6585#page-3
    status = 429

    # If the request is an AJAX on, we return a plain short string.
    # Also, if the request is coming from something like curl, it will
    # send the header `Accept: */*`. But if you take the same URL and open
    # it in the browser it'll look something like:
    # `Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8`
    if (
        request.is_ajax() or
        'text/html' not in request.META.get('HTTP_ACCEPT', '')
    ):
        # Return a super spartan message.
        # We could also do something like `{"error": "Too Many Requests"}`
        return http.HttpResponse(
            'Too Many Requests',
            status=status,
            content_type='text/plain'
        )

    return render(request, 'crashstats/ratelimit_blocked.html', status=status)


def robots_txt(request):
    return http.HttpResponse(
        'User-agent: *\n'
        '%s: /' % ('Allow' if settings.ENGAGE_ROBOTS else 'Disallow'),
        content_type='text/plain',
    )


def build_id_to_date(build_id):
    yyyymmdd = str(build_id)[:8]
    return '{}-{}-{}'.format(
        yyyymmdd[:4],
        yyyymmdd[4:6],
        yyyymmdd[6:8],
    )


@csp_update(CONNECT_SRC='analysis-output.telemetry.mozilla.org')
@pass_default_context
def report_index(request, crash_id, default_context=None):
    valid_crash_id = utils.find_crash_id(crash_id)
    if not valid_crash_id:
        return http.HttpResponseBadRequest('Invalid crash ID')

    # Sometimes, in Socorro we use a prefix on the crash ID. Usually it's
    # 'bp-' but this is configurable.
    # If you try to use this to reach the perma link for a crash, it should
    # redirect to the report index with the correct crash ID.
    if valid_crash_id != crash_id:
        return redirect(reverse('crashstats:report_index', args=(valid_crash_id,)))

    context = default_context or {}
    context['crash_id'] = crash_id

    refresh_cache = request.GET.get('refresh') == 'cache'

    raw_api = models.RawCrash()
    try:
        context['raw'] = raw_api.get(crash_id=crash_id, refresh_cache=refresh_cache)
    except CrashIDNotFound:
        # If the raw crash can't be found, we can't do much.
        return render(request, 'crashstats/report_index_not_found.html', context, status=404)
    utils.enhance_raw(context['raw'])

    context['your_crash'] = (
        request.user.is_active and
        context['raw'].get('Email') == request.user.email
    )

    api = models.UnredactedCrash()
    try:
        context['report'] = api.get(crash_id=crash_id, refresh_cache=refresh_cache)
    except CrashIDNotFound:
        # ...if we haven't already done so.
        cache_key = 'priority_job:{}'.format(crash_id)
        if not cache.get(cache_key):
            priority_api = models.Priorityjob()
            priority_api.post(crash_ids=[crash_id])
            cache.set(cache_key, True, 60)
        return render(request, 'crashstats/report_index_pending.html', context)

    if 'json_dump' in context['report']:
        json_dump = context['report']['json_dump']
        if 'sensitive' in json_dump and not request.user.has_perm('crashstats.view_pii'):
            del json_dump['sensitive']
        context['raw_stackwalker_output'] = json.dumps(
            json_dump,
            sort_keys=True,
            indent=4,
            separators=(',', ': ')
        )
        utils.enhance_json_dump(json_dump, settings.VCS_MAPPINGS)
        parsed_dump = json_dump
    else:
        context['raw_stackwalker_output'] = 'No dump available'
        parsed_dump = {}

    context['crashing_thread'] = parsed_dump.get('crash_info', {}).get('crashing_thread')
    if context['report']['signature'].startswith('shutdownhang'):
        # For shutdownhang signatures, we want to use thread 0 as the
        # crashing thread, because that's the thread that actually contains
        # the useful data about what happened.
        context['crashing_thread'] = 0

    context['parsed_dump'] = parsed_dump
    context['bug_product_map'] = settings.BUG_PRODUCT_MAP

    bugs_api = models.Bugs()
    hits = bugs_api.get(signatures=[context['report']['signature']])['hits']
    # bugs_api.get(signatures=...) will return all signatures associated
    # with the bugs found, but we only want those with matching signature
    context['bug_associations'] = [
        x for x in hits
        if x['signature'] == context['report']['signature']
    ]
    context['bug_associations'].sort(key=lambda x: x['id'], reverse=True)

    context['raw_keys'] = []
    if request.user.has_perm('crashstats.view_pii'):
        # hold nothing back
        context['raw_keys'] = context['raw'].keys()
    else:
        context['raw_keys'] = [
            x for x in context['raw']
            if x in models.RawCrash.API_WHITELIST()
        ]
    # Sort keys case-insensitively
    context['raw_keys'].sort(key=lambda s: s.lower())

    if request.user.has_perm('crashstats.view_rawdump'):
        context['raw_dump_urls'] = [
            reverse('crashstats:raw_data', args=(crash_id, 'dmp')),
            reverse('crashstats:raw_data', args=(crash_id, 'json'))
        ]
        if context['raw'].get('additional_minidumps'):
            suffixes = [
                x.strip()
                for x in context['raw']['additional_minidumps'].split(',')
                if x.strip()
            ]
            for suffix in suffixes:
                name = 'upload_file_minidump_%s' % (suffix,)
                context['raw_dump_urls'].append(
                    reverse('crashstats:raw_data_named', args=(crash_id, name, 'dmp'))
                )
        if (
            context['raw'].get('ContainsMemoryReport') and
            context['report'].get('memory_report') and
            not context['report'].get('memory_report_error')
        ):
            context['raw_dump_urls'].append(
                reverse('crashstats:raw_data_named', args=(crash_id, 'memory_report', 'json.gz'))
            )

    # Add descriptions to all fields.
    all_fields = SuperSearchFields().get()
    descriptions = {}
    for field in all_fields.values():
        key = '{}.{}'.format(field['namespace'], field['in_database_name'])
        descriptions[key] = '{} Search: {}'.format(
            field.get('description', '').strip() or 'No description for this field.',
            field['is_exposed'] and field['name'] or 'N/A',
        )

    def make_raw_crash_key(key):
        """In the report_index.html template we need to create a key
        that we can use to look up against the 'fields_desc' dict.
        Because you can't do something like this in jinja::

            {{ fields_desc.get(u'raw_crash.{}'.format(key), empty_desc) }}

        we do it here in the function instead.
        The trick is that the lookup key has to be a unicode object or
        else you get UnicodeEncodeErrors in the template rendering.
        """
        return u'raw_crash.{}'.format(key)

    context['make_raw_crash_key'] = make_raw_crash_key
    context['fields_desc'] = descriptions
    context['empty_desc'] = 'No description for this field. Search: unknown'

    context['BUG_PRODUCT_MAP'] = settings.BUG_PRODUCT_MAP

    # report.addons used to be a list of lists.
    # In https://bugzilla.mozilla.org/show_bug.cgi?id=1250132
    # we changed it from a list of lists to a list of strings, using
    # a ':' to split the name and version.
    # See https://bugzilla.mozilla.org/show_bug.cgi?id=1250132#c7
    # Considering legacy, let's tackle both.
    # In late 2017, this code is going to be useless and can be removed.
    if (
        context['report'].get('addons') and
        isinstance(context['report']['addons'][0], (list, tuple))
    ):
        # This is the old legacy format. This crash hasn't been processed
        # the new way.
        context['report']['addons'] = [
            ':'.join(x) for x in context['report']['addons']
        ]

    return render(request, 'crashstats/report_index.html', context)


def status_json(request):
    """This is deprecated and should not be used.
    Use the /api/Status/ endpoint instead.
    """
    if settings.DEBUG:
        raise Exception(
            'This view is deprecated and should not be accessed. '
            'The only reason it\'s kept is for legacy reasons.'
        )
    return redirect(reverse('api:model_wrapper', args=('Status',)))


def dockerflow_version(requst):
    path = os.path.join(settings.SOCORRO_ROOT, 'version.json')
    if os.path.exists(path):
        with open(path, 'r') as fp:
            data = fp.read()
    else:
        data = '{}'
    return http.HttpResponse(data, content_type='application/json')


@pass_default_context
def crontabber_state(request, default_context=None):
    context = default_context or {}
    return render(request, 'crashstats/crontabber_state.html', context)


@pass_default_context
def login(request, default_context=None):
    context = default_context or {}
    return render(request, 'crashstats/login.html', context)


def quick_search(request):
    query = request.GET.get('query', '').strip()
    crash_id = utils.find_crash_id(query)

    if crash_id:
        url = reverse(
            'crashstats:report_index',
            kwargs=dict(crash_id=crash_id)
        )
    elif query:
        url = '%s?signature=%s' % (
            reverse('supersearch:search'),
            urlquote('~%s' % query)
        )
    else:
        url = reverse('supersearch:search')

    return redirect(url)


@utils.json_view
def buginfo(request, signatures=None):
    form = forms.BugInfoForm(request.GET)
    if not form.is_valid():
        return http.HttpResponseBadRequest(str(form.errors))

    bug_ids = form.cleaned_data['bug_ids']

    bzapi = models.BugzillaBugInfo()
    result = bzapi.get(bug_ids)
    return result


@permission_required('crashstats.view_rawdump')
def raw_data(request, crash_id, extension, name=None):
    api = models.RawCrash()
    if extension == 'json':
        format = 'meta'
        content_type = 'application/json'
    elif extension == 'dmp':
        format = 'raw'
        content_type = 'application/octet-stream'
    elif extension == 'json.gz' and name == 'memory_report':
        # Note, if the name is 'memory_report' it will fetch a raw
        # crash with name and the files in the memory_report bucket
        # are already gzipped.
        # This is important because it means we don't need to gzip
        # the HttpResponse below.
        format = 'raw'
        content_type = 'application/octet-stream'
    else:
        raise NotImplementedError(extension)

    data = api.get(crash_id=crash_id, format=format, name=name)
    response = http.HttpResponse(content_type=content_type)
    if extension == 'json':
        response.write(json.dumps(data))
    else:
        response.write(data)
    return response


@pass_default_context
def about_throttling(request, default_context=None):
    """Return a simple page that explains about how throttling works."""
    context = default_context or {}
    return render(request, 'crashstats/about_throttling.html', context)


@pass_default_context
def new_report_index(request, crash_id, default_context=None):
    context = default_context or {}
    return render(request, 'crashstats/new_report_index.html', context)


@pass_default_context
def home(request, default_context=None):
    context = default_context or {}
    return render(request, 'crashstats/home.html', context)


@pass_default_context
def product_home(request, product, default_context=None):
    context = default_context or {}

    # Figure out versions
    if product not in context['products']:
        raise http.Http404('Not a recognized product')

    if product in context['active_versions']:
        context['versions'] = [
            x['version']
            for x in context['active_versions'][product]
            if x['is_featured']
        ]
        # If there are no featured versions but there are active
        # versions, then fall back to use that instead.
        if not context['versions'] and context['active_versions'][product]:
            # But when we do that, we have to make a manual cut-off of
            # the number of versions to return. So make it max 4.
            context['versions'] = [
                x['version']
                for x in context['active_versions'][product]
            ][:settings.NUMBER_OF_FEATURED_VERSIONS]
    else:
        context['versions'] = []

    return render(request, 'crashstats/product_home.html', context)
