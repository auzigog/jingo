"""Adapter for using Jinja2 with Django."""
import functools
import imp
import logging

from django import http
from django.core.cache import cache
from django.conf import settings
from django.template.context import get_standard_processors, Context
from django.template import Origin
from django.utils.importlib import import_module
from django.utils.translation import trans_real
from django.utils.encoding import force_unicode

import jinja2
from hashlib import md5

VERSION = (0, 4, 6)
__version__ = '.'.join(map(str, VERSION))

log = logging.getLogger('jingo')


_helpers_loaded = False


class Template(jinja2.Template):
    def __new__(cls, source, *args, **kwargs):
        #use the shared env instead of constructing an ad-hoc one for template from source so helpers work
        return env.from_string(source, template_class=cls)

    def render(self, context):
        # flatten the Django Context into a single dictionary.
        context_dict = {}
        if isinstance(context, Context):
            for d in getattr(context, 'dicts', []):
                context_dict.update(d)
        else:
            context_dict.update(context)

        if settings.TEMPLATE_DEBUG:
            from django.test import signals
            self.origin = Origin(self.filename)
            signals.template_rendered.send(sender=self, template=self, context=context)

        return super(Template, self).render(context_dict)


class Environment(jinja2.Environment):

    def get_template(self, name, parent=None, globals=None):
        """Make sure our helpers get loaded before any templates."""
        load_helpers()
        return super(Environment, self).get_template(name, parent, globals)

    def from_string(self, source, globals=None, template_class=None):
        load_helpers()
        return super(Environment, self).from_string(source, globals,
                                                    template_class)


def get_env():
    """Configure and return a jinja2 Environment."""
    # Mimic Django's setup by loading templates from directories in
    # TEMPLATE_DIRS and packages in INSTALLED_APPS.
    x = ((jinja2.FileSystemLoader, settings.TEMPLATE_DIRS),
         (jinja2.PackageLoader, settings.INSTALLED_APPS))
    loaders = [loader(p) for loader, places in x for p in places]

    opts = {'trim_blocks': True,
            'extensions': ['jinja2.ext.i18n'],
            'autoescape': True,
            'auto_reload': settings.DEBUG,
            'loader': jinja2.ChoiceLoader(loaders),
            }

    if hasattr(settings, 'JINJA_CONFIG'):
        if hasattr(settings.JINJA_CONFIG, '__call__'):
            config = settings.JINJA_CONFIG()
        else:
            config = settings.JINJA_CONFIG
        opts.update(config)

    e = Environment(**opts)
    e.template_class = Template
    if 'jinja2.ext.i18n' in e.extensions:
        # TODO: use real translations
        e.install_null_translations()
    return e


def render(request, template, context=None, **kwargs):
    """
    Shortcut like Django's ``render_to_response``, but better.

    Minimal usage, with only a request object and a template name::

        return jingo.render(request, 'template.html')

    With template context and keywords passed to
    :class:`django.http.HttpResponse`::

        return jingo.render(request, 'template.html',
                            {'some_var': 42}, status=209)
    """
    rendered = render_to_string(request, template, context)
    return http.HttpResponse(rendered, **kwargs)


def render_to_string(request, template, context=None):
    """
    Render a template into a string.
    """
    def get_context():
        # Only call the context processors once per request.
        if not hasattr(request, '_jingo_context'):
            request._jingo_context = {}
            for processor in get_standard_processors():
                request._jingo_context.update(processor(request))
        ctx = {} if context is None else context.copy()
        ctx.update(request._jingo_context)
        return ctx

    # If it's not a Template, it must be a path to be loaded.
    if not isinstance(template, jinja2.environment.Template):
        template = env.get_template(template)

    return template.render(context=get_context())


def load_helpers():
    """Try to import ``helpers.py`` from each app in INSTALLED_APPS."""
    # We want to wait as long as possible to load helpers so there aren't any
    # weird circular imports with jingo.
    global _helpers_loaded
    if _helpers_loaded:
        return
    _helpers_loaded = True

    from jingo import helpers

    for app in settings.INSTALLED_APPS:
        try:
            app_path = import_module(app).__path__
        except AttributeError:
            continue

        try:
            imp.find_module('helpers', app_path)
        except ImportError:
            continue

        import_module('%s.helpers' % app)


class Register(object):
    """Decorators to add filters and functions to the template Environment."""

    def __init__(self, env):
        self.env = env

    def filter(self, f):
        """Adds the decorated function to Jinja's filter library."""
        self.env.filters[f.__name__] = f
        return f

    def function(self, f):
        """Adds the decorated function to Jinja's global namespace."""
        self.env.globals[f.__name__] = f
        return f

    def inclusion_tag(self, template):
        """Adds a function to Jinja, but like Django's @inclusion_tag."""
        def decorator(f):
            @functools.wraps(f)
            def wrapper(*args, **kw):
                context = f(*args, **kw)
                t = env.get_template(template).render(context)
                return jinja2.Markup(t)
            return self.function(wrapper)
        return decorator

    def cached_inclusion_tag(self, template, key, kwargs=True):
        """
        Adds a function to Jinja, but like Django's @inclusion_tag.
        Caches the rendered template in 'key'.

        All keyword arguments passed to the function are hashed together and form part of the key
        """
        def decorator(f):
            @functools.wraps(f)
            def wrapper(*args, **kw):
                if kwargs and (args or kw):
                    key_ = key + "_" + md5('_'.join([
                        '_'.join([str(a) for a in args]),
                        '_'.join(['%s_%s' % (k,v,) for k,v in kw.items()]),
                    ])).hexdigest()
                else:
                    key_ = key

                t = cache.get(key_)
                if t is None:
                    context = f(*args, **kw)
                    t = env.get_template(template).render(context)
                    cache.set(key_, t)

                return jinja2.Markup(t)
            return self.function(wrapper)
        return decorator


env = get_env()
register = Register(env)
