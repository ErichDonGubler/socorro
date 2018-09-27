# -*- coding: utf-8 -*-
# Generated by Django 1.11.15 on 2018-09-27 13:22
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crashstats', '0003_1457747_graphics_data_migration'),
    ]

    operations = [
        migrations.CreateModel(
            name='Signature',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('signature', models.TextField(help_text=b'the crash report signature', unique=True)),
                ('first_build', models.BigIntegerField(help_text=b'the first build id this signature was seen in')),
                ('first_date', models.DateTimeField(help_text=b'the first crash report date this signature was seen in')),
            ],
        ),
    ]
