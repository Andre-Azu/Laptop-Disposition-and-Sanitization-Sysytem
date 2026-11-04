from django import template

from ..utils import format_kes


register = template.Library()


@register.filter(name="kes_format")
def kes_format(value):
    return format_kes(value)
