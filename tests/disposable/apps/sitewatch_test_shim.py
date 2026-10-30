"""Disposable tests only: point site-update and the collector at the fake GitHub API."""

from sitewatch import apps, updater

apps.API = "http://127.0.0.1:8099"
updater.github_host = lambda url: url.startswith("http://127.0.0.1:8099/")
