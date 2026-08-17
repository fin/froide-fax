"""URLconf for the tests.

froide's own urlconf does not include this package, so nothing in
``froide_fax.urls`` is reachable -- and ``get_signed_media_url`` reverses
``froide_fax-media_url``. Mount both so views and reverse() work.
"""

from django.urls import include, path

from froide.urls import urlpatterns as froide_urlpatterns

urlpatterns = froide_urlpatterns + [
    path("fax/", include("froide_fax.urls")),
]
