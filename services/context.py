from extensions import cache
from models import SiteSettings
from flask_login import current_user
from flask import g


def inject_settings():
    inst_id = None
    if current_user.is_authenticated:
        inst_id = getattr(current_user, 'institution_id', None)
    cache_key = f'site_settings_{inst_id}' if inst_id else 'site_settings_global'
    settings = cache.get(cache_key)
    if not settings:
        orig_ignore = getattr(g, 'ignore_tenant_filter', False)
        g.ignore_tenant_filter = True
        try:
            if inst_id:
                settings = SiteSettings.query.filter_by(institution_id=inst_id).first()
            else:
                settings = SiteSettings.query.filter_by(institution_id=None).first()
            if not settings:
                settings = SiteSettings.query.first()
        finally:
            g.ignore_tenant_filter = orig_ignore
            
        if settings:
            cache.set(cache_key, settings, timeout=60)
    return dict(settings=settings)
