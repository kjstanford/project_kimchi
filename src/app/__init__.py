"""Web app over the measure package: check a config, preview its probe plan, run it, browse saved data.

Layout (ARCHITECTURE.md): api.py routes only, pipeline.py one entry function per endpoint, models.py the typed
request/response models (the /docs contract), runs.py the one active run and its events.
"""
