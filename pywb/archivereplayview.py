from pywb.webapp.handlers import WBHandler
from pywb.utils.statusandheaders import StatusAndHeaders
from pywb.webapp.replay_views import ReplayView, CaptureException
from pywb.rewrite.url_rewriter import UrlRewriter
from pywb.rewrite.rewrite_live import LiveRewriter
from pywb.utils.timeutils import timestamp_to_sec
from pywb.utils.loaders import BlockLoader

from redisclient import redisclient

import requests
import re
import logging

import xml.etree.ElementTree as ElementTree
import urlparse
import json
import os


#=============================================================================
WBURL_RX = re.compile('(.*/)([0-9]{1,14})(\w{2}_)?/(https?://.*)')
EXTRACT_ORIG_LINK = re.compile(r'<([^>]+)>;\s*rel=\"original\"')

NO_GZIP_UAS = ['NCSA_Mosaic']


#=============================================================================
class ReplayHandler(WBHandler):
    def _init_replay_view(self, config):
        return ReplayView(UpstreamArchiveLoader(config), config)


#=============================================================================
class MementoHandler(ReplayHandler):
    def _init_replay_view(self, config):
        return ReplayView(MementoUpstreamArchiveLoader(config), config)

    def handle_query(self, wbrequest, cdx_lines, output):
        try:
            offset = int(wbrequest.wb_url.timestamp)
            if offset < 1:
                offset = 1
        except Exception as e:
            offset = 1

        cdx_json = None
        if output != 'text':
            cdx_lines = list(cdx_lines)

            try:
                cdx_json = [dict(host=cdx['src_host'], ts=cdx['timestamp']) for cdx in cdx_lines]
                cdx_json = json.dumps(cdx_json)
            except Exception as e:
                logging.debug(e)

            cdx_lines = iter(cdx_lines)

        return self.index_reader.make_cdx_response(wbrequest,
                                                   cdx_lines,
                                                   output,
                                                   offset=offset,
                                                   cdx_json=cdx_json)


#=============================================================================
class UpstreamArchiveLoader(object):
    def __init__(self, config):
        self.session = requests.Session()
        self.session.max_redirects = 6
        self.archive_template = config['archive_template']
        self.archive_name = config['archive_name']
        self.reverse_proxy_prefix = config.get('reverse_proxy_prefix', '')

        # init redis here only
        redisclient.init_redis(config)

    def unrewrite_header(self, response, name):
        value = response.headers.get(name)

        # extract orig url from redirect, if any
        if value:
            m = WBURL_RX.match(value)
            if m:
                response.headers[name] = m.group(4)

    def _do_req(self, urls, host, env, skip_hosts):
        response = None

        headers = {}
        user_agent = env.get('HTTP_USER_AGENT', '')

        # disable gzip, as mosaic won't support it!
        # TODO: maybe ungzip later
        if any(exclude in user_agent for exclude in NO_GZIP_UAS):
            headers={'Accept-Encoding': 'identity'}

        for url in urls:
            if self.reverse_proxy_prefix:
                url = self.reverse_proxy_prefix + url

            response = self.session.request(method='GET',
                                            url=url,
                                            allow_redirects=False,
                                            headers=headers,
                                            stream=True,
                                            verify=False)

            if response is None:
                continue

            mem_date_time = response.headers.get('memento-datetime')

            if (response.status_code >= 400 and not mem_date_time):
                # assume temp failure, can try again
                if response.status_code == 503:
                    return None

                elif response.status_code == 403 or response.status_code >= 500:
                    # don't try again
                    skip_hosts.append(host)
                    return None

                 # try again
                continue

            # success
            return response

        return response

    def __call__(self, cdx, skip_hosts, cdx_loader, wbrequest):
        self.session.cookies.clear()

        try_urls, host, archive_name = self._get_urls_to_try(cdx, skip_hosts, wbrequest)

        try:
            response = self._do_req(try_urls, host, wbrequest.env, skip_hosts)
        except Exception as e:
            print(e)
            response = None

        if response is None:
            print(skip_hosts)
            raise CaptureException('Content Could Not Be Loaded')

        if response.status_code >= 300 and response.status_code < 400:
            self.unrewrite_header(response, 'Location')
            self.unrewrite_header(response, 'Content-Location')

        remote = wbrequest.env.get('REMOTE_ADDR')
        req_ts = wbrequest.wb_url.timestamp
        base_key = remote + ':' + req_ts

        sec = timestamp_to_sec(cdx['timestamp'])
        referrer = wbrequest.env.get('HTTP_REFERER')

        try:
            pi = redisclient.redis.pipeline(transaction=False)

            pi.hset(base_key + ':urls', cdx['url'], sec)
            pi.sadd(base_key + ':hosts', archive_name)

            if referrer and not referrer.endswith('.css'):
                pi.set(base_key + ':ref', referrer)
            elif not referrer:
                pi.set(base_key + ':base', cdx['url'])

            pi.execute()
        except Exception as e:
            import traceback
            traceback.print_exc(e)

        statusline = str(response.status_code) + ' ' + response.reason

        headers = response.headers.items()

        stream = response.raw

        status_headers = StatusAndHeaders(statusline, headers)

        return (status_headers, stream)

    def _get_urls_to_try(self, cdx, skip_hosts, wbrequest):
        if self.archive_template in skip_hosts:
            raise Exception('Content Not Available')

        #full_url = self.archive_template + wbrequest.coll + '/' + cdx['timestamp'] + 'id_/' + url
        full_url = self.archive_template.format(timestamp=cdx['timestamp'],
                                                url=cdx['url'])

        try_urls = [full_url]
        return try_urls, self.archive_template, self.archive_name


#=============================================================================
class MementoUpstreamArchiveLoader(UpstreamArchiveLoader):
    def __init__(self, config):
        super(MementoUpstreamArchiveLoader, self).__init__(config)
        if config.get('memento_archive_json'):
            self.load_archive_info_json(config.get('memento_archive_json'))
        else:
            self.load_archive_info_xml(config.get('memento_archive_xml'))

    def load_archive_info_json(self, url):
        self.archive_infos = {}
        url = os.path.expandvars(url)
        logging.debug('Loading XML from {0}'.format(url))
        if not url:
            return

        try:
            stream = BlockLoader().load(url)
        except Exception as e:
            logging.debug(e)
            logging.debug('Proceeding without json archive info')
            return

        archives = json.loads(stream.read())
        for arc in archives:
            id_ = arc['id']
            name = arc['name']
            uri = arc['timegate']
            unrewritten_url = arc.get('unrewritten_url')
            if not unrewritten_url:
                unrewritten_url = uri + '{timestamp}id_/{url}'

            self.archive_infos[id_] = {'id': id_,
                                       'uri': uri,
                                       'name': name,
                                       'rewritten': True,
                                       'unrewritten_url': unrewritten_url}


    def load_archive_info_xml(self, url):
        self.archive_infos = {}
        url = os.path.expandvars(url)
        logging.debug('Loading XML from {0}'.format(url))
        if not url:
            return

        try:
            stream = BlockLoader().load(url)
        except Exception as e:
            logging.debug(e)
            logging.debug('Proceeding without xml archive info')
            return

        root = ElementTree.fromstring(stream.read())

        for link in root.findall('link'):
            name = link.get('id')
            longname = link.get('longname')
            archive = link.find('archive')
            timegate = link.find('timegate')

            if timegate is None or archive is None:
                continue

            rewritten = (archive.get('rewritten-urls') == 'yes')
            unrewritten_url = archive.get('un-rewritten-api-url', '')
            uri = timegate.get('uri')

            self.archive_infos[name] = {'uri': uri,
                                        'rewritten': rewritten,
                                        'unrewritten_url': unrewritten_url,
                                        'name': longname
                                        }

    def find_archive_info(self, uri):
        #uri = uri.split('://', 1)[-1]
        uri = uri.replace(':80/', '/')
        for name, info in self.archive_infos.iteritems():
            if info['uri'] in uri:
                return info
        return None


    def _get_urls_to_try(self, cdx, skip_hosts, wbrequest):
        src_url = cdx['src_url']
        parts = urlparse.urlsplit(src_url)

        if src_url in skip_hosts:
            raise CaptureException('Skipping already failed: ' + src_url)

        archive_host = parts.netloc

        info = self.find_archive_info(src_url)

        if info and info.get('unrewritten_url'):
            orig_url = info['unrewritten_url'].format(archive_host=archive_host,
                                                      timestamp=cdx['timestamp'],
                                                      url=cdx['url'])
            try_urls = [orig_url]
        else:
            try_urls = [src_url]

        if info:
            name = info.get('id', src_url)
        else:
            name = src_url

        wbrequest.urlrewriter.rewrite_opts['orig_src_url'] = cdx['src_url']
        wbrequest.urlrewriter.rewrite_opts['archive_info'] = info
        return try_urls, src_url, name

