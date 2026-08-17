import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publicbody", "__first__"),
        ("froide_fax", "0003_faxpermission"),
    ]

    operations = [
        migrations.CreateModel(
            name="FaxOverride",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Uncheck to fall back to email without deleting this entry.",
                        verbose_name="enabled",
                    ),
                ),
                (
                    "fax_number",
                    models.CharField(
                        blank=True,
                        help_text="Leave blank to use the public body's own fax number.",
                        max_length=50,
                        verbose_name="fax number override",
                    ),
                ),
                ("note", models.TextField(blank=True, verbose_name="note")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "publicbody",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fax_override",
                        to="publicbody.publicbody",
                        verbose_name="public body",
                    ),
                ),
            ],
            options={
                "verbose_name": "fax override",
                "verbose_name_plural": "fax overrides",
                "ordering": ("publicbody__name",),
            },
        ),
    ]
