import base64
import os

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from froide.helper.storage import HashedFilenameStorage
from froide.publicbody.models import PublicBody

DATA_URL_PNG = "data:image/png;base64,"


def signature_path(instance=None, filename=None):
    path = ["signatures", filename]
    return os.path.join(*path)


FAX_PERMISSION_PART = "can_always_fax"
FAX_PERMISSION = "froide_fax." + FAX_PERMISSION_PART


class FaxPermission(models.Model):
    """This model has not database table, it is used to define permissions for fax sending"""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = ((FAX_PERMISSION_PART, _("Can always send fax messages")),)


class Signature(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, verbose_name=_("User")
    )
    signature = models.ImageField(
        null=True, blank=True, upload_to=signature_path, storage=HashedFilenameStorage()
    )
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = _("Signature")
        verbose_name_plural = _("Signatures")

    def __str__(self):
        return str(self.user)

    def remove_signature_file(self):
        if not self.signature:
            return
        self.signature.delete()

    def get_signature_dataurl(self):
        if not self.signature:
            return None
        signature_bytes = self.get_signature_bytes()
        if not signature_bytes:
            return None
        b64_string = base64.b64encode(signature_bytes).decode("utf-8")
        return DATA_URL_PNG + b64_string

    def get_signature_bytes(self):
        if not self.signature:
            return None
        try:
            self.signature.open()
        except IOError:
            # File was deleted, set field to None
            self.signature = None
            self.save()
            return None
        try:
            return self.signature.read()
        finally:
            self.signature.close()


class FaxOverrideManager(models.Manager):
    def get_for_publicbody(self, publicbody):
        """Return a usable FaxOverride for `publicbody`, or None.

        Usable means enabled *and* resolving to a dialable number. An enabled
        override we cannot dial must not divert the request away from email.
        """
        if publicbody is None:
            return None
        try:
            override = publicbody.fax_override
        except FaxOverride.DoesNotExist:
            return None
        if not override.is_usable:
            return None
        return override

    def is_fax_recipient(self, publicbody):
        return self.get_for_publicbody(publicbody) is not None


class FaxOverride(models.Model):
    """Marks a public body as "fax instead of email".

    This is the opt-in for the second faxing mode. The mode this package was
    originally built for sends a signed fax *in addition to* the email, gated
    on FoiLaw.requires_signature. This one replaces the email entirely, for
    authorities that will not accept electronic requests at all.

    The number normally comes from the existing PublicBody.fax field;
    fax_number here is only an escape hatch for when that number is wrong or a
    separate FOI fax line exists.
    """

    publicbody = models.OneToOneField(
        PublicBody,
        on_delete=models.CASCADE,
        related_name="fax_override",
        verbose_name=_("public body"),
    )
    enabled = models.BooleanField(
        _("enabled"),
        default=True,
        help_text=_("Uncheck to fall back to email without deleting this entry."),
    )
    fax_number = models.CharField(
        _("fax number override"),
        max_length=50,
        blank=True,
        help_text=_("Leave blank to use the public body's own fax number."),
    )
    note = models.TextField(_("note"), blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = FaxOverrideManager()

    class Meta:
        verbose_name = _("fax override")
        verbose_name_plural = _("fax overrides")
        ordering = ("publicbody__name",)

    def __str__(self):
        return "%s (%s)" % (self.publicbody, self.number or _("no number"))

    @property
    def number(self):
        from .utils import parse_fax_number

        return parse_fax_number(self.fax_number or self.publicbody.fax)

    @property
    def is_usable(self):
        return self.enabled and bool(self.number)

    def clean(self):
        from django.core.exceptions import ValidationError

        from .utils import parse_fax_number

        if self.fax_number and parse_fax_number(self.fax_number) is None:
            raise ValidationError(
                {"fax_number": _("This is not a usable fax number.")}
            )
        if self.enabled and not self.number:
            raise ValidationError(
                {
                    "fax_number": _(
                        "This public body has no usable fax number, so one must "
                        "be given here for the override to have any effect."
                    )
                }
            )
