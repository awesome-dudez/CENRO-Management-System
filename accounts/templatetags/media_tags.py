from django import template

from cenro_mgmt.media_utils import file_url_if_exists as _file_url_if_exists

register = template.Library()


@register.filter
def file_url_if_exists(fieldfile):
    """Same as ``cenro_mgmt.media_utils.file_url_if_exists`` for templates."""
    return _file_url_if_exists(fieldfile)
