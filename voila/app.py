#############################################################################
# Copyright (c) 2018, Voila Contributors                                    #
#                                                                           #
# Distributed under the terms of the BSD 3-Clause License.                  #
#                                                                           #
# The full license is in the file LICENSE, distributed with this software.  #
#############################################################################

from zmq.eventloop import ioloop

ioloop.install()

import os
import shutil
import tempfile
import json
import logging
import gettext

import jinja2

import tornado.ioloop
import tornado.web

from traitlets.config.application import Application
from traitlets import Unicode, Integer, Bool, default

from jupyter_server.services.kernels.kernelmanager import MappingKernelManager
from jupyter_server.services.kernels.handlers import KernelHandler, ZMQChannelsHandler
from jupyter_server.base.handlers import path_regex
from jupyter_server.services.contents.largefilemanager import LargeFileManager
from jupyter_server.utils import url_path_join
from jupyter_core.paths import jupyter_path
from .paths import ROOT, STATIC_ROOT
from .handler import VoilaHandler
from .treehandler import VoilaTreeHandler
from ._version import __version__
from .static_file_handler import MultiStaticFileHandler

_kernel_id_regex = r"(?P<kernel_id>\w+-\w+-\w+-\w+-\w+)"


class Voila(Application):
    name = 'voila'
    version = __version__
    examples = 'voila example.ipynb --port 8888'
    description = Unicode(
        """voila [OPTIONS] NOTEBOOK_FILENAME

        This launches a stand-alone server for read-only notebooks.
        """
    )
    option_description = Unicode(
        """
        notebook_path:
            File name of the Jupyter notebook to display.
        """
    )
    notebook_filename = Unicode()
    strip_sources = Bool(True, help='Strip sources from rendered html').tag(config=True)
    port = Integer(
        8866,
        config=True,
        help='Port of the voila server. Default 8866.'
    )
    autoreload = Bool(
        False,
        config=True,
        help='Will autoreload to server and the page when a template, js file or Python code changes'
    )
    static_root = Unicode(
        STATIC_ROOT,
        config=True,
        help='Directory holding static assets (HTML, JS and CSS files).'
    )
    aliases = {
        'port': 'Voila.port',
        'static': 'Voila.static_root',
        'strip_sources': 'Voila.strip_sources',
        'autoreload': 'Voila.autoreload',
        'template': 'Voila.template'
    }
    connection_dir_root = Unicode(
        config=True,
        help=(
            'Location of temporry connection files. Defaults '
            'to system `tempfile.gettempdir()` value.'
        )
    )
    connection_dir = Unicode()

    template = Unicode(
        'default',
        config=True,
        allow_none=True,
        help=(
            'template name to be used by voila.'
        )
    )

    @default('connection_dir_root')
    def _default_connection_dir(self):
        return tempfile.gettempdir()
        connection_dir = tempfile.mkdtemp()
        self.log.info('Using %s to store connection files' % connection_dir)
        return connection_dir

    @default('log_level')
    def _default_log_level(self):
        return logging.INFO

    def parse_command_line(self, argv=None):
        super(Voila, self).parse_command_line(argv)
        self.notebook_path = self.extra_args[0] if len(self.extra_args) == 1 else None
        self.nbconvert_template_paths = []
        self.template_paths = []
        self.static_paths = [self.static_root]
        if self.template:
            self._collect_template_paths(self.template)
            self.log.debug('using template: %s', self.template)
            self.log.debug('nbconvert template paths: %s', self.nbconvert_template_paths)
            self.log.debug('template paths: %s', self.template_paths)
            self.log.debug('static paths: %s', self.static_paths)

    def _collect_template_paths(self, template_name):
        # we look at the usual jupyter locations, and for development purposes also
        # relative to the package directory (with highest precedence)
        template_directories = \
            [os.path.abspath(os.path.join(ROOT, '..', 'share', 'jupyter', 'voila', 'template', template_name))] +\
            jupyter_path('voila', 'template', template_name)
        for dirname in template_directories:
            if os.path.exists(dirname):
                conf = {}
                conf_file = os.path.join(dirname, 'conf.json')
                if os.path.exists(conf_file):
                    with open(conf_file) as f:
                        conf = json.load(f)
                # for templates that are not named default, we assume the default base_template is 'default'
                # that means that even the default template could have a base_template when explicitly given
                if template_name != 'default' or 'base_template' in conf:
                    self._collect_template_paths(conf.get('base_template', 'default'))

                extra_nbconvert_path = os.path.join(dirname, 'nbconvert_templates')
                if not os.path.exists(extra_nbconvert_path):
                    self.log.warning('template named %s found at path %r, but %s does not exist', self.template,
                                     dirname, extra_nbconvert_path)
                self.nbconvert_template_paths.insert(0, extra_nbconvert_path)

                extra_static_path = os.path.join(dirname, 'static')
                if not os.path.exists(extra_static_path):
                    self.log.warning('template named %s found at path %r, but %s does not exist', self.template,
                                     dirname, extra_static_path)
                self.static_paths.insert(0, extra_static_path)

                extra_template_path = os.path.join(dirname, 'templates')
                if not os.path.exists(extra_template_path):
                    self.log.warning('template named %s found at path %r, but %s does not exist', self.template,
                                     dirname, extra_template_path)
                self.template_paths.insert(0, extra_template_path)

                # we don't look at multiple directories, once a directory with a given name is found at a particular
                # location (for instance the user dir) don't look further (for instance sys.prefix)
                break

    def start(self):
        connection_dir = tempfile.mkdtemp(
            prefix='voila_',
            dir=self.connection_dir_root
        )
        self.log.info('Storing connection files in %s.' % connection_dir)
        self.log.info('Serving static files from %s.' % self.static_root)

        kernel_manager = MappingKernelManager(
            connection_dir=connection_dir,
            allowed_message_types=[
                'comm_msg',
                'comm_info_request',
                'kernel_info_request',
                'shutdown_request'
            ]
        )

        jenv_opt = {"autoescape": True}  # we might want extra options via cmd line like notebook server
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(self.template_paths), extensions=['jinja2.ext.i18n'], **jenv_opt)
        nbui = gettext.translation('nbui', localedir=os.path.join(ROOT, 'i18n'), fallback=True)
        env.install_gettext_translations(nbui, newstyle=False)
        contents_manager = LargeFileManager()  # TODO: make this configurable like notebook

        webapp = tornado.web.Application(
            kernel_manager=kernel_manager,
            allow_remote_access=True,
            autoreload=self.autoreload,
            voila_jinja2_env=env,
            jinja2_env=env,
            static_path='/',
            server_root_dir='/',
            contents_manager=contents_manager
        )

        base_url = webapp.settings.get('base_url', '/')

        handlers = []

        handlers.extend([
            (url_path_join(base_url, r'/api/kernels/%s' % _kernel_id_regex), KernelHandler),
            (url_path_join(base_url, r'/api/kernels/%s/channels' % _kernel_id_regex), ZMQChannelsHandler),
            (
                url_path_join(base_url, r'/voila/static/(.*)'),
                MultiStaticFileHandler,
                {
                    'paths': self.static_paths,
                    'default_filename': 'index.html'
                }
            )
        ])

        if self.notebook_path:
            handlers.append((
                url_path_join(base_url, r'/'),
                VoilaHandler,
                {
                    'notebook_path': self.notebook_path,
                    'strip_sources': self.strip_sources,
                    'nbconvert_template_paths': self.nbconvert_template_paths,
                    'config': self.config
                }
            ))
        else:
            handlers.extend([
                (base_url, VoilaTreeHandler),
                (url_path_join(base_url, r'/voila/tree' + path_regex), VoilaTreeHandler),
                (url_path_join(base_url, r'/voila/render' + path_regex), VoilaHandler, {'strip_sources': self.strip_sources}),
            ])

        webapp.add_handlers('.*$', handlers)

        webapp.listen(self.port)
        self.log.info('Voila listening on port %s.' % self.port)

        try:
            tornado.ioloop.IOLoop.current().start()
        finally:
            shutil.rmtree(connection_dir)

main = Voila.launch_instance
