A `local_settings.py` file can be placed here to override or extend the settings
bundled with DefectDojo. This folder is ignored by Git and Docker.

If the file is present, startup copies it to
`dojo/settings/local_settings.py`.

For an example, see [template-local_settings](../../vendor/defectdojo/dojo/settings/template-local_settings)

This copy can fail when the full `dojo/` directory is mounted with a different
owner. It therefore runs only in the Docker Compose release mode, not in the
development, debug, unit-test, or integration-test modes.

For advanced usage you can also place a `settings.dist.py` or `settings.py` file. These will also be copied on startup to dojo/settings.

The nginx container does not use files from `docker/extra_settings`; its
settings are required at image build time.
