#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# PCDS Devices documentation build configuration file, created by
# sphinx-quickstart on Mon Apr  3 21:34:53 2017.
#
# This file is execfile()d with the current directory set to its
# containing dir.
#
# Note that not all possible configuration values are present in this
# autogenerated file.
#
# All configuration values have a default; values that are commented out
# serve to show the default.

import datetime
import enum
import os
import re
import sys
import typing

import ophyd
import sphinx_rtd_theme
from docutils import statemachine
from docutils.parsers.rst import Directive, directives

import pcdsdevices
import pcdsdevices.component

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
module_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),'../../')
sys.path.insert(0, module_path)


# -- General configuration ------------------------------------------------

# If your documentation needs a minimal Sphinx version, state it here.
#
# needs_sphinx = '1.0'

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = ['sphinx.ext.autodoc',
              'sphinx.ext.todo',
              'sphinx.ext.autosummary',
              'sphinx.ext.intersphinx',
              'sphinx.ext.napoleon',
              'IPython.sphinxext.ipython_directive',
              'IPython.sphinxext.ipython_console_highlighting',
              'sphinx.ext.autosectionlabel'
             ]

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

autosummary_generate = True

# The suffix(es) of source filenames.
# You can specify multiple suffix as a list of string:
source_suffix = '.rst'

# The master toctree document.
master_doc = 'index'

# General information about the project.
project = 'PCDS Devices'
year = datetime.datetime.now().year
copyright = '{}, SLAC National Accelerator Laboratory'.format(year)
author = 'SLAC National Accelerator Laboratory'

# The version info for the project you're documenting, acts as replacement for
# |version| and |release|, also used in various other places throughout the
# built documents.
#
# The short X.Y version.
version = pcdsdevices.__version__
# The full version, including alpha/beta/rc tags.
release = pcdsdevices.__version__

# The language for content autogenerated by Sphinx. Refer to documentation
# for a list of supported languages.
#
# This is also used if you do content translation via gettext catalogs.
# Usually you set "language" from the command line for these cases.
language = None

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This patterns also effect to html_static_path and html_extra_path
exclude_patterns = []

# The reST default role (used for this markup: `text`)
default_role = 'any'

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = 'sphinx'

# If true, `todo` and `todoList` produce output, else they produce nothing.
todo_include_todos = False

# If true, return types are displayed on a separate line.
napoleon_use_rtype = False

# -- Options for HTML output ----------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'sphinx_rtd_theme'

# Theme options are theme-specific and customize the look and feel of a theme
# further.  For a list of options available for each theme, see the
# documentation.
#
# html_theme_options = {}

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
# html_static_path = ['_static']
html_static_path = []


suppress_warnings = [
    'autosectionlabel.releases',
]


# -- Options for HTMLHelp output ------------------------------------------

# Output file base name for HTML help builder.
htmlhelp_basename = 'PCDSDevicesdoc'


# -- Options for LaTeX output ---------------------------------------------

latex_elements = {
    # The paper size ('letterpaper' or 'a4paper').
    #
    # 'papersize': 'letterpaper',

    # The font size ('10pt', '11pt' or '12pt').
    #
    # 'pointsize': '10pt',

    # Additional stuff for the LaTeX preamble.
    #
    # 'preamble': '',

    # Latex figure (float) alignment
    #
    # 'figure_align': 'htbp',
}

# Grouping the document tree into LaTeX files. List of tuples
# (source start file, target name, title,
#  author, documentclass [howto, manual, or own class]).
latex_documents = [
    (master_doc, 'PCDSDevices.tex', 'PCDS Devices Documentation',
     'SLAC National Accelerator Laboratory', 'manual'),
]


# -- Options for manual page output ---------------------------------------

# One entry per manual page. List of tuples
# (source start file, name, description, authors, manual section).
man_pages = [
    (master_doc, 'pcdsdevices', 'PCDS Devices Documentation',
     [author], 1)
]


# -- Options for Texinfo output -------------------------------------------

# Grouping the document tree into Texinfo files. List of tuples
# (source start file, target name, title, author,
#  dir menu entry, description, category)
texinfo_documents = [
    (master_doc, 'PCDSDevices', 'PCDS Devices Documentation',
     author, 'PCDSDevices', 'ophyd Devices used at the LCLS',
     'Miscellaneous'),
]

# -- Sources of external documentation to cross-referencing----------------

intersphinx_mapping = {'ophyd': ('https://blueskyproject.io/ophyd', None),
                       'python': ('https://docs.python.org/3', None),
                       'numpy': ('https://docs.scipy.org/doc/numpy', None)}



OPHYD_SKIP = {
    # Methods
    'add_instantiation_callback',
    'check_value',
    'clear_sub',
    # 'configure',
    # 'describe',
    'describe_configuration',
    'destroy',
    'format_status_info',
    # 'get',
    'get_device_tuple',
    'get_instantiated_signals',
    'pause',
    'put',  # prefer `set`
    # 'read',
    # 'read_configuration',
    'resume',
    'stage',
    'status_info',
    # 'stop',
    'subscribe',
    # 'summary',
    # 'trigger',
    'unstage',
    'unsubscribe',
    'unsubscribe_all',
    'wait_for_connection',
    'walk_components',
    'walk_signals',
    'walk_subdevice_classes',
    'walk_subdevices',

    # Attributes
    'SUB_ACQ_DONE',
    'SUB_STATE',
    'SUB_DONE',
    'SUB_READBACK',
    'SUB_START',
    'SUB_VALUE',
    'attr_name',
    'component_names',
    # 'configuration_attrs',
    # 'connected',
    'dotted_name',
    'event_types',
    # 'hints',
    'inserted',
    # 'kind',
    'lazy_wait_for_connection',
    # 'lightpath_cpts',
    'name',
    'parent',
    'read_attrs',
    'removed',
    'report',
    'root',
    'signal_names',
    'tab_component_names',
    'tab_whitelist',
    'trigger_signals',
}


def skip_components_and_ophyd_stuff(app, what, name, obj, skip, options):
    if isinstance(obj, ophyd.Component):
        return True
    if name.startswith('_'):
        # It's unclear if I broke this or if it's always been broken,
        # but for our use case we never want to document `_` items with
        # autoclass.
        return True
    if name in OPHYD_SKIP:
        return True
    return skip


short_component_names = {
    ophyd.Component: '',
    ophyd.DynamicDeviceComponent: 'DDC',
    ophyd.FormattedComponent: 'FCpt',
    pcdsdevices.component.UnrelatedComponent: 'UCpt',
}


def _get_class_info(cls):
    if cls is None:
        return None

    return {
        'name': cls.__name__,
        'class': cls,
        'link': f':class:`~{cls.__module__}.{cls.__name__}`'
    }


def _dynamic_device_component_to_row(base_attrs, cls, attr, cpt):
    cpt_type = short_component_names.get(type(cpt), type(cpt).__name__)

    doc = cpt.doc or ''
    if doc.startswith('DynamicDeviceComponent attribute') :
        doc = ''

    nested_components = [
        _component_to_row(base_attrs, cls, attr, dynamic_cpt)
        for attr, dynamic_cpt in cpt.components.items()
    ]

    return dict(
        component=cpt,
        attr=attr if not cpt_type else f'{attr} ({cpt_type})',
        cls=_get_class_info(getattr(cpt, 'cls', None)),
        nested_components=nested_components,
        doc=doc,
        kind=cpt.kind.name,
        inherited_from=_get_class_info(base_attrs.get(attr, None)),
    )


def _component_to_row(base_attrs, cls, attr, cpt):
    if isinstance(cpt, ophyd.DynamicDeviceComponent):
        return _dynamic_device_component_to_row(base_attrs, cls, attr, cpt)

    cpt_type = short_component_names.get(type(cpt),
                                              type(cpt).__name__)

    doc = cpt.doc or ''
    if doc.startswith(f'{cpt.__class__.__name__} attribute') :
        doc = ''

    return dict(
        component=cpt,  # access to the component instance itself
        attr=attr if not cpt_type else f'{attr} ({cpt_type})',
        cls=_get_class_info(getattr(cpt, 'cls', None)),
        suffix=f'``{cpt.suffix}``' if cpt.suffix else '',
        doc=doc,
        kind=cpt.kind.name,
        inherited_from=_get_class_info(base_attrs.get(attr, None)),
    )


def _get_base_attrs(cls):
    base_devices = [
        base for base in reversed(cls.__bases__)
        if hasattr(base, '_sig_attrs')
    ]

    return {
        attr: base
        for base in base_devices
        for attr, cpt in base._sig_attrs.items()
    }


# NOTE: can't use functools.lru_cache here as it's not picklable
_device_cache = {}


def get_device_info(module, name):
    class_name = f'{module}.{name}'
    if class_name in _device_cache:
        return _device_cache[class_name]

    module_name, class_name = class_name.rsplit('.', 1)
    module = __import__(module_name, globals(), locals(), [class_name])
    cls = getattr(module, class_name)

    if not issubclass(cls, ophyd.Device):
        info = []
    else:
        base_attrs = _get_base_attrs(cls)

        info = [
            _component_to_row(base_attrs, cls, attr, cpt)
            for attr, cpt in cls._sig_attrs.items()
        ]

    _device_cache[class_name] = info
    return info


autosummary_context = {
    # Allow autosummary/class.rst to do its magic:
    'get_device_info': get_device_info,
}

html_context = {
    'css_files': [
        '_static/theme_overrides.css',  # override wide tables in RTD theme
    ],
}


def rstjinja(app, docname, source):
    """
    Render our pages as a jinja template for fancy templating goodness.
    """
    # Borrowed from
    # https://www.ericholscher.com/blog/2016/jul/25/integrating-jinja-rst-sphinx/
    # Make sure we're outputting HTML
    if app.builder.format != 'html':
        return

    src = source[0]
    rendered = app.builder.templates.render_string(src,
                                                   app.config.html_context)
    source[0] = rendered


def setup(app):
    app.connect('autodoc-skip-member', skip_components_and_ophyd_stuff)
    app.connect("source-read", rstjinja)
