#!/usr/bin/env python3
"""Minimal ESO Logs GraphQL user-API client."""
import requests
from auth import ESOLogsOAuth

URL = 'https://www.esologs.com/api/v2/user'


def query(q, variables=None):
    r = requests.post(URL, json={'query': q, 'variables': variables or {}},
                      headers={'Authorization': ESOLogsOAuth.auth_header()})
    r.raise_for_status()
    data = r.json()
    if 'errors' in data:
        raise RuntimeError(data['errors'])
    return data['data']


if __name__ == '__main__':
    print(query('{ userData { currentUser { id name } } }'))
