#!/usr/bin/env python3
"""Quick operational check for local or containerized deployments."""

import json
import os
import sys
from urllib import error, request


def resolve_urls() -> tuple[str, str]:
    explicit = os.environ.get('HOSPITAL_API_BASE', '').strip()
    if explicit:
        explicit = explicit.rstrip('/')
        if explicit.endswith('/api'):
            return explicit, explicit[:-4]
        return f'{explicit}/api', explicit

    frontend_base = os.environ.get('HOSPITAL_FRONTEND_BASE', 'http://localhost:8080').strip().rstrip('/')
    return f'{frontend_base}/api', frontend_base


def fetch(url: str):
    with request.urlopen(url, timeout=10) as response:
        payload = response.read().decode('utf-8')
        try:
            return response.status, json.loads(payload)
        except json.JSONDecodeError:
            return response.status, payload


def main():
    api_base, root_base = resolve_urls()
    targets = [
        ('health', f'{api_base}/health'),
        ('deep_health', f'{api_base}/health/deep'),
        ('dashboard', f'{api_base}/dashboard'),
        ('metrics', f'{root_base}/metrics'),
    ]
    failed = False

    for name, url in targets:
        try:
            status, payload = fetch(url)
            print(f'[{name}] {status}')
            if isinstance(payload, dict):
                print(json.dumps(payload, indent=2))
        except error.URLError as exc:
            failed = True
            print(f'[{name}] error: {exc}')

    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
